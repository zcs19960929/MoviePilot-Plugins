"""Small, lazy 115 Cookie adapter. No 115Lite imports or interactive login."""
import re
import sys
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit


class CloudError(RuntimeError):
    pass


class AuthenticationError(CloudError):
    pass


class RiskError(CloudError):
    pass


class Stopped(CloudError):
    pass


@dataclass(frozen=True)
class Entry:
    id: str
    parent_id: str
    name: str
    is_dir: bool = False
    pickcode: str = ""
    size: int = 0
    sha1: str = ""


@dataclass(frozen=True)
class DirectLink:
    url: str
    headers: dict
    expires: float


class Cloud115:
    def __init__(self, cookie, interval=5, stop_event=None, sdk=None):
        parsed = SimpleCookie()
        try:
            parsed.load(cookie or "")
            uid = parsed["UID"].value.split("_")[0]
            valid = uid.isdigit() and all(parsed.get(k) for k in ("CID", "SEID"))
        except Exception:
            valid = False
        if not valid:
            raise AuthenticationError("请填写有效的 115 Cookie（UID、CID、SEID）")
        self.account = uid
        self.stop_event = stop_event or threading.Event()
        self.interval = max(0, float(interval))
        self._lock = threading.RLock()
        self._last_request = 0.0
        if sdk is None:
            if sys.version_info < (3, 12):
                raise CloudError("115 直连模式需要 Python 3.12 或更高版本；原有模式仍可使用")
            try:
                from p115client import P115Client
                sdk = P115Client(cookie)
            except Exception:
                raise CloudError("115 SDK 无法加载，请检查插件依赖安装及兼容版本") from None
        self.sdk = sdk

    def _wait(self):
        delay = max(0, self.interval - (time.monotonic() - self._last_request))
        if self.stop_event.wait(delay):
            raise Stopped("任务已停止")
        self._last_request = time.monotonic()

    @staticmethod
    def _check(response):
        if isinstance(response, dict) and response.get("state") in (False, 0):
            code = str(response.get("errno") or response.get("errNo") or response.get("errcode")
                       or response.get("code") or response.get("error_code") or "unknown")
            if code in {"911", "590075", "40101004", "429", "405"}:
                raise RiskError("115 要求验证或限制请求，请停止任务后检查账户")
            if code in {"99", "401", "40101032", "40101017"}:
                raise AuthenticationError("115 Cookie 已失效，请更新后重试")
            raise CloudError("115 接口返回错误代码 " + code)
        return response

    def _call(self, method, *args, background=True, **kwargs):
        # All SDK errors are bounded messages: never echo exception URLs/Cookies.
        with self._lock:
            if background:
                self._wait()
            try:
                result = getattr(self.sdk, method)(*args, timeout=30, **kwargs)
            except Exception as exc:
                # High-level SDK methods validate internally and attach their JSON
                # response to OSError.args; preserve risk classification, not its text.
                for argument in exc.args:
                    if isinstance(argument, dict):
                        self._check(argument)
                status = (getattr(getattr(exc, "response", None), "status_code", None)
                          or getattr(exc, "status", None) or getattr(exc, "code", None))
                if status in (405, 429) or "Risk" in type(exc).__name__:
                    raise RiskError("115 请求受到限制，请检查账户状态") from None
                if status in (401, 403) or any(name in type(exc).__name__ for name in ("Login", "Authentication")):
                    raise AuthenticationError("115 Cookie 无效或权限不足") from None
                raise CloudError("115 请求失败（" + type(exc).__name__ + "）") from None
            return self._check(result)

    @staticmethod
    def _entry(raw):
        if "is_dir" in raw:
            is_dir = bool(raw["is_dir"])
        elif "file_category" in raw:
            is_dir = str(raw["file_category"]) == "0"
        elif "fc" in raw:
            is_dir = str(raw["fc"]) == "0"
        elif "sha" in raw:
            is_dir = not raw["sha"]
        elif "sha1" in raw:
            is_dir = not raw["sha1"]
        else:
            is_dir = "fid" not in raw and "file_id" not in raw
        fid = raw.get("id") or raw.get("fid") or raw.get("file_id") or raw.get("cid")
        parent = raw.get("parent_id")
        if parent is None:
            parent = raw.get("pid") if is_dir else raw.get("cid")
        if parent is None:
            parent = raw.get("category_id", "0")
        name = raw.get("name") or raw.get("n") or raw.get("fn") or raw.get("file_name") or raw.get("category_name")
        if not fid or not name:
            raise CloudError("115 返回的文件信息不完整")
        return Entry(str(fid), str(parent), str(name), is_dir,
                     str(raw.get("pickcode") or raw.get("pc") or raw.get("pick_code") or ""),
                     int(raw.get("size") or raw.get("s") or raw.get("fs") or raw.get("file_size") or 0),
                     str(raw.get("sha1") or raw.get("sha") or ""))

    def check(self):
        self._call("fs_files", {"cid": 0, "cur": 1, "show_dir": 1, "limit": 1, "offset": 0})
        return self.account

    def listdir(self, cid):
        cid, offset, count = str(cid), 0, None
        entries, seen = [], set()
        while True:
            result = self._call("fs_files", {"cid": cid, "cur": 1, "show_dir": 1,
                                            "limit": 500, "offset": offset})
            breadcrumbs = result.get("path") or []
            if breadcrumbs and str(breadcrumbs[-1].get("cid", breadcrumbs[-1].get("id"))) != cid:
                raise CloudError("115 返回了不同目录，已停止扫描")
            total = int(result.get("count", 0))
            if count is not None and total != count:
                raise CloudError("目录在分页扫描期间发生变化，请稍后重试")
            count = total
            page = result.get("data") or []
            if not isinstance(page, list):
                raise CloudError("115 文件列表格式错误")
            added = 0
            for raw in page:
                entry = self._entry(raw)
                if entry.parent_id != cid:
                    raise CloudError("115 文件父目录与请求不符，已停止扫描")
                if entry.id not in seen:
                    entries.append(entry)
                    seen.add(entry.id)
                    added += 1
            offset += len(page)
            if offset >= count:
                return entries
            if not page or not added:
                raise CloudError("115 分页没有前进，已停止扫描")

    def info(self, fid):
        # Unlike fs_file_skim, get_info includes the parent ID required to
        # verify directory identity and resumable rename operations.
        response = self._call("fs_file", {"file_id": str(fid)})
        data = response.get("data") or []
        if not data:
            raise CloudError("115 文件不存在或不可访问")
        entry = self._entry(data[0] if isinstance(data, list) else data)
        if entry.id != str(fid):
            raise CloudError("115 返回了不同的文件 ID")
        return entry

    def resolve_folder(self, value):
        value = str(value).strip()
        if value.startswith("id:"):
            value = value[3:]
        if value.isdigit():
            if value != "0" and not self.info(value).is_dir:
                raise CloudError("扫描目标不是文件夹")
            return value
        if not value.startswith("/"):
            raise CloudError("网盘目录需使用绝对路径或 id:目录ID")
        cid = "0"
        for part in value.split("/"):
            if not part:
                continue
            if part in (".", ".."):
                raise CloudError("网盘路径包含无效目录段")
            matches = [e for e in self.listdir(cid) if e.is_dir and e.name == part]
            if len(matches) != 1:
                raise CloudError("目录不存在或有同名目录，请使用目录 ID")
            cid = matches[0].id
        return cid

    def rename(self, fid, name):
        if not name or name in (".", "..") or any(c in name for c in "/\\\r\n\0"):
            raise CloudError("目标文件名无效")
        self._call("fs_rename", (str(fid), name))

    def download_link(self, entry, user_agent="", background=False):
        if not entry.pickcode:
            raise CloudError("文件缺少 pickcode，无法获取下载链接")
        result = self._call("download_url", entry.pickcode, user_agent=user_agent,
                            app="android", background=background)
        url = str(result)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            raise CloudError("115 返回了无效下载地址")
        headers = dict(getattr(result, "headers", {}) or {})
        try:
            expires = float(parse_qs(parts.query).get("t", [time.time() + 60])[0])
        except (ValueError, TypeError):
            expires = time.time() + 60
        return DirectLink(url, headers, expires)

    def download_bytes(self, entry):
        import requests
        link = self.download_link(entry, "MoviePilot-MetaTubeScraper/1.1.0", background=True)
        with self._lock:
            self._wait()
            try:
                with requests.get(link.url, headers=link.headers, timeout=60,
                                  stream=True, allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise CloudError("字幕下载失败，请检查 115 直链权限")
                    chunks, length = [], 0
                    for chunk in response.iter_content(65536):
                        if self.stop_event.is_set():
                            raise Stopped("任务已停止")
                        length += len(chunk)
                        if length > 64 * 1024 * 1024:
                            raise CloudError("字幕超过 64 MiB，已停止下载")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if not data:
                        raise CloudError("字幕下载结果为空")
                    return data
            except CloudError:
                raise
            except Exception:
                raise CloudError("字幕下载请求失败") from None
