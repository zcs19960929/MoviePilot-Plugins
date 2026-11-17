"""百度网盘 Web 客户端。"""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
from threading import RLock
from typing import Any, Dict, Mapping, Optional

import requests
from app.log import logger

from ..common import DriveRateLimiter, format_size, safe_int


def is_success(response: Any) -> bool:
    """按 errno/error_code 判断成功。"""
    if not isinstance(response, Mapping):
        return False
    if "error_code" in response and "errno" not in response:
        return safe_int(response.get("error_code")) == 0
    return safe_int(response.get("errno")) == 0


class BaiduPanClient:
    """基于 Cookie 的百度网盘客户端。"""

    is_success = staticmethod(is_success)
    BASE_URL = "https://pan.baidu.com"
    PCS_URL = "https://d.pcs.baidu.com"
    PASSPORT_URL = "https://passport.baidu.com"
    # 百度网盘 Web 渠道标识
    DEFAULT_PARAMS = {
        "channel": "chunlei",
        "clienttype": 0,
        "app_id": 250528,
        "web": 1,
    }
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://pan.baidu.com/disk/main",
        "Origin": "https://pan.baidu.com",
    }
    DOWNLOAD_HEADERS = {
        "User-Agent": (
            "netdisk;P2SP;3.0.0.8;netdisk;11.19.2;PC;PC-Windows;10.0.17763;"
            "WindowsBaiduYunGuanJia"
        ),
        "Referer": "https://pan.baidu.com/",
    }
    TEMPLATE_FIELDS = (
        "bdstoken", "uk", "username", "photo",
        "is_vip", "is_svip",
    )
    ERROR_MESSAGES = {
        -6: "百度登录已失效，请更新网盘 Cookie（包含 BDUSS、STOKEN）",
        -7: "百度分享已失效或文件已删除",
        -9: "百度分享提取码错误",
        -12: "百度分享需要提取码或验证",
        -30: "目标目录已有同名文件或文件夹",
        -62: "百度提取码尝试次数过多，请稍后重试",
        -63: "百度分享访问受限，请稍后重试",
        4: "目标目录已有相同文件",
        12: "百度文件操作部分失败或超过账号单次转存限制",
        105: "百度分享已取消",
        115: "百度分享已过期",
        130: "百度网盘空间不足",
        132: "百度账号需要安全验证，请在网页完成验证后重试",
        31024: "百度网盘空间不足",
        31034: "百度接口请求过于频繁，请稍后重试",
        31045: "百度登录状态验证未通过，请重新登录",
        31326: "百度账号需要验证，请在网页完成验证后重试",
        8001: "百度账号触发验证，请在网页完成验证后重试",
        20012: "百度接口调用次数已达上限，请稍后重试",
        20013: "百度应用权限不足，请完成网页验证或重新授权",
    }
    QRCODE_TTL = 120
    QRCODE_MAX_SESSIONS = 32
    _qrcode_sessions: Dict[str, Dict[str, Any]] = {}
    _qrcode_lock = RLock()

    def __init__(self, cookie: str = "", timeout: int = 30):
        self._timeout = max(5, min(int(timeout or 30), 300))
        self._session = requests.Session()
        self._session.headers.update(self.DEFAULT_HEADERS)
        self._lock = RLock()
        self._token = ""
        self._profile: Dict[str, Any] = {}
        self._token_expires_at = 0.0
        for part in str(cookie or "").split(";"):
            key, separator, value = part.partition("=")
            key = key.strip()
            if key and separator:
                self._session.cookies.set(
                    key, value.strip(), domain=".baidu.com", path="/"
                )
        self.rate_limiter = DriveRateLimiter.shared(
            "baidu", cookie, min_interval=0.8
        )

    @property
    def cookie(self) -> str:
        return "; ".join(
            f"{item.name}={item.value}"
            for item in self._session.cookies
            if item.name and item.value is not None
        )

    @property
    def request_timeout(self) -> int:
        return self._timeout

    def close(self) -> None:
        self._session.close()

    def set_cookie(self, key: str, value: str) -> None:
        """写入单个 Cookie。"""
        name = str(key or "").strip()
        for item in list(self._session.cookies):
            if item.name == name:
                self._session.cookies.clear(item.domain, item.path, item.name)
        self._session.cookies.set(name, str(value or ""), domain=".baidu.com", path="/")

    def cookie_value(self, key: str) -> str:
        name = str(key or "").strip()
        for item in self._session.cookies:
            if item.name == name:
                return str(item.value or "")
        return ""

    @classmethod
    def error_message(cls, response: Mapping[str, Any]) -> str:
        code = safe_int(response.get("errno", -1))
        return cls.ERROR_MESSAGES.get(code) or f"百度接口请求失败（错误码 {code}）"

    @staticmethod
    def _json_payload(response: requests.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            text = (response.text or "").strip()
            login_page = "<html" in text[:200].lower()
            return {
                "errno": -6 if login_page else -1,
                "message": (
                    "百度网盘登录状态已失效，请重新获取 Cookie"
                    if login_page
                    else (text[:300] or f"HTTP {response.status_code}")
                ),
            }
        if isinstance(payload, Mapping):
            return dict(payload)
        return {"errno": -1, "message": "百度网盘返回格式无效"}

    def request(
            self,
            method: str,
            path: str,
            *,
            params: Optional[Mapping[str, Any]] = None,
            data: Optional[Mapping[str, Any]] = None,
            json_data: Optional[Any] = None,
            files: Optional[Mapping[str, Any]] = None,
            headers: Optional[Mapping[str, str]] = None,
            base_url: Optional[str] = None,
            timeout: Any = None,
            allow_redirects: bool = False,
            with_bdstoken: bool = False,
    ) -> Dict[str, Any]:
        base = str(base_url or self.BASE_URL).rstrip("/")
        url = path if str(path).startswith("http") else f"{base}/{str(path).lstrip('/')}"
        query = {**self.DEFAULT_PARAMS, **dict(params or {})}
        if with_bdstoken:
            token = self.bdstoken
            if not token:
                return {"errno": -6, "message": self.ERROR_MESSAGES[-6]}
            query["bdstoken"] = token
        request_headers = dict(self.DEFAULT_HEADERS)
        if headers:
            request_headers.update({str(key): str(value) for key, value in headers.items()})
        kwargs: Dict[str, Any] = {
            "params": query,
            "headers": request_headers,
            "timeout": self._timeout if timeout is None else timeout,
            "allow_redirects": allow_redirects,
        }
        if data is not None:
            kwargs["data"] = data
        if json_data is not None:
            kwargs["json"] = json_data
        if files is not None:
            kwargs["files"] = files
        try:
            response = self.rate_limiter.call(
                self._session.request,
                str(method or "GET").upper(),
                url,
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
                **kwargs,
            )
        except requests.RequestException as error:
            logger.error(f"百度网盘请求失败：{path} - {type(error).__name__}")
            return {
                "errno": -1,
                "message": "百度网盘请求失败或超时，请检查网络后重试",
            }
        if response.status_code in (301, 302, 401, 403):
            result: Dict[str, Any] = {"errno": -6}
        elif response.status_code >= 400:
            result = {
                "errno": -1,
                "message": f"百度接口返回 HTTP {response.status_code}",
            }
        else:
            result = self._json_payload(response)
        result.setdefault("http_status", response.status_code)
        if "errno" not in result and "error_code" in result:
            result["errno"] = result.get("error_code")
        if not self.is_success(result):
            if safe_int(result.get("errno")) == -6:
                with self._lock:
                    self._token = ""
                    self._token_expires_at = 0.0
            result.setdefault("message", self.error_message(result))
            logger.debug(
                f"百度网盘接口返回错误：{path} "
                f"errno={result.get('errno')} message={result.get('message') or ''}"
            )
        return result

    def _get_template(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if (
                    not force
                    and self._token
                    and time.monotonic() < self._token_expires_at
            ):
                return dict(self._profile)
        response = self.request(
            "GET",
            "/api/gettemplatevariable",
            params={
                "fields": json.dumps(
                    list(self.TEMPLATE_FIELDS), separators=(",", ":")
                )
            },
        )
        result = response.get("result") if self.is_success(response) else None
        if not isinstance(result, Mapping) or not str(result.get("bdstoken") or "").strip():
            raise RuntimeError(response.get("message") or self.ERROR_MESSAGES[-6])
        with self._lock:
            self._profile = dict(result)
            self._token = str(result.get("bdstoken") or "")
            self._token_expires_at = time.monotonic() + 300
            return dict(self._profile)

    @property
    def bdstoken(self) -> str:
        try:
            return str(self._get_template().get("bdstoken") or "").strip()
        except RuntimeError:
            return ""

    def check_login(self) -> bool:
        try:
            self._get_template(force=True)
            return True
        except RuntimeError:
            return False

    def get_quota(self) -> Dict[str, Any]:
        return self.request(
            "GET",
            "/api/quota",
            params={"checkfree": 1, "checkexpire": 1},
            with_bdstoken=True,
        )

    def get_account_info(self) -> Dict[str, Any]:
        try:
            profile = self._get_template(force=True)
        except RuntimeError as error:
            return {"connected": False, "error": str(error)}
        quota = self.get_quota()
        total = safe_int(quota.get("total")) if self.is_success(quota) else 0
        used = safe_int(quota.get("used")) if self.is_success(quota) else 0
        is_svip = bool(safe_int(profile.get("is_svip")))
        is_vip = is_svip or bool(safe_int(profile.get("is_vip")))
        vip_label = "超级会员" if is_svip else ("会员" if is_vip else "普通用户")
        return {
            "connected": True,
            "user": {
                "name": str(profile.get("username") or "百度网盘用户"),
                "avatar": str(profile.get("photo") or ""),
                "membership_supported": True,
                "is_vip": is_vip,
                "vip_label": vip_label,
                "is_forever_vip": False,
                "vip_expire_date": "",
            },
            "storage": {
                "total": format_size(total),
                "used": format_size(used),
                "remaining": format_size(max(0, total - used)),
            },
        }

    @classmethod
    def download_headers(cls) -> Dict[str, str]:
        return dict(cls.DOWNLOAD_HEADERS)

    @staticmethod
    def _payload(response: requests.Response) -> Dict[str, Any]:
        response.raise_for_status()
        text = response.text.strip()
        match = re.fullmatch(r"[\w.$]+\s*\((.*)\)\s*;?", text, re.S)
        value = json.loads(match.group(1) if match else text)
        if not isinstance(value, dict):
            raise RuntimeError("百度扫码接口返回格式异常")
        return value

    @classmethod
    def _discard_qrcode(cls, token: str) -> None:
        item = cls._qrcode_sessions.pop(token, None)
        if item:
            item["session"].close()

    @classmethod
    def _expire_qrcode_sessions(cls) -> None:
        for token, item in list(cls._qrcode_sessions.items()):
            if item["expires"] <= time.monotonic():
                cls._discard_qrcode(token)
        while len(cls._qrcode_sessions) >= cls.QRCODE_MAX_SESSIONS:
            cls._discard_qrcode(next(iter(cls._qrcode_sessions)))

    @classmethod
    def close_qrcode_sessions(cls) -> None:
        with cls._qrcode_lock:
            for token in list(cls._qrcode_sessions):
                cls._discard_qrcode(token)

    _qrcode_blocked_until = 0.0

    @staticmethod
    def _qrcode_gid() -> str:
        raw = secrets.token_hex(16).upper()
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"

    @staticmethod
    def _qrcode_log_page() -> str:
        return f"traceId:pc_loginv5_{int(time.time())},logPage:loginv5"

    @staticmethod
    def _qrcode_headers(referer: str) -> Dict[str, str]:
        return {
            "User-Agent": BaiduPanClient.DEFAULT_HEADERS["User-Agent"],
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
        }

    @classmethod
    def _qrcode_risk(cls, data: Dict[str, Any]) -> str:
        text = " ".join(str(data.get(key) or "") for key in (
            "errno", "message", "show_msg", "errmsg",
        )).lower()
        markers = ("频繁", "风控", "验证", "限制", "too many", "risk", "blocked")
        return text if any(marker in text for marker in markers) else ""

    @classmethod
    def create_qrcode_login(cls, client_type: str = "") -> Dict[str, Any]:
        with cls._qrcode_lock:
            if time.monotonic() < cls._qrcode_blocked_until:
                raise RuntimeError("百度扫码登录已触发风控，请等待约 10 分钟后再试")
            cls._expire_qrcode_sessions()
            session = requests.Session()
            try:
                now = int(time.time() * 1000)
                gid = cls._qrcode_gid()
                callback = f"tangram_guid_{now}"
                log_page = cls._qrcode_log_page()
                session.get(
                    f"{cls.BASE_URL}/",
                    headers=cls._qrcode_headers("https://pan.baidu.com/"),
                    timeout=15,
                )
                data = cls._payload(session.get(
                    f"{cls.PASSPORT_URL}/v2/api/getqrcode",
                    params={
                        "lp": "pc",
                        "qrloginfrom": "pc",
                        "gid": gid,
                        "callback": callback,
                        "apiver": "v3",
                        "tt": now,
                        "tpl": "netdisk",
                        "logPage": log_page,
                        "_": now,
                    },
                    headers=cls._qrcode_headers("https://pan.baidu.com/"),
                    timeout=15,
                ))
                sign = str(data.get("sign") or "")
                if str(data.get("errno", 0)) != "0" or not sign:
                    if cls._qrcode_risk(data):
                        cls._qrcode_blocked_until = time.monotonic() + 600
                        raise RuntimeError("百度扫码登录触发风控，请等待约 10 分钟后再试")
                    raise RuntimeError("百度未返回登录二维码，请稍后重试")
                image_url = str(data.get("imgurl") or "").strip()
                if image_url and not image_url.startswith(("http://", "https://")):
                    image_url = f"https://{image_url}"
                if not image_url:
                    image_url = f"{cls.PASSPORT_URL}/v2/api/qrcode?sign={sign}&lp=pc"
                image = session.get(
                    image_url,
                    headers=cls._qrcode_headers(
                        "https://passport.baidu.com/v3/login/qrcodepage?tpl=netdisk"
                    ),
                    timeout=15,
                )
                image.raise_for_status()
                if not image.content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError("百度登录二维码图片无效，请稍后重试")
                token = secrets.token_urlsafe(32)
                cls._qrcode_sessions[token] = {
                    "session": session,
                    "sign": sign,
                    "gid": gid,
                    "callback": callback,
                    "polling": False,
                    "expires": time.monotonic() + cls.QRCODE_TTL,
                }
                return {
                    "qr_token": token,
                    "interval": 30,
                    "qrcode": (
                            "data:image/png;base64,"
                            + base64.b64encode(image.content).decode("ascii")
                    ),
                }
            except Exception:
                session.close()
                raise

    @classmethod
    def check_qrcode_login(cls, qr_token: str = "", **kwargs: Any) -> Dict[str, Any]:
        with cls._qrcode_lock:
            item = cls._qrcode_sessions.get(qr_token)
            if not item or item["expires"] <= time.monotonic():
                cls._discard_qrcode(qr_token)
                return {"status": "expired", "message": "二维码已过期，请刷新后重新扫码"}
            if item.get("polling"):
                return {"status": "waiting", "message": "正在检查扫码状态"}
            item["polling"] = True
            session = item["session"]
            sign = item["sign"]
            gid = item.get("gid") or cls._qrcode_gid()
            callback = item.get("callback") or f"tangram_guid_{int(time.time() * 1000)}"
        try:
            now = int(time.time() * 1000)
            data = cls._payload(session.get(
                f"{cls.PASSPORT_URL}/channel/unicast",
                params={
                    "channel_id": sign,
                    "gid": gid,
                    "tpl": "netdisk",
                    "_sdkFrom": 1,
                    "callback": callback,
                    "apiver": "v3",
                    "tt": now,
                    "_": now,
                },
                headers=cls._qrcode_headers(
                    "https://passport.baidu.com/v3/login/qrcodepage?tpl=netdisk"
                ),
                timeout=30,
            ))
            errno = str(data.get("errno", 0))
            if errno == "1":
                return {"status": "waiting", "message": "等待百度网盘 App 扫码确认"}
            if errno != "0":
                risk = cls._qrcode_risk(data)
                cls._discard_qrcode(qr_token)
                if risk:
                    cls._qrcode_blocked_until = time.monotonic() + 600
                    return {
                        "status": "expired",
                        "message": "百度扫码登录触发风控，请等待约 10 分钟后再试",
                        "risk": True,
                    }
                return {"status": "expired", "message": "二维码已失效，请重新扫码"}
            channel = data.get("channel_v") or {}
            if isinstance(channel, str):
                channel = json.loads(channel)
            if not isinstance(channel, dict):
                raise RuntimeError("百度扫码状态格式异常")
            if not channel:
                return {"status": "waiting", "message": "等待百度网盘 App 扫码确认"}
            status = str(channel.get("status", ""))
            if status in {"1", "2"}:
                return {"status": "waiting", "message": "请在百度网盘 App 中确认登录"}
            if status != "0":
                cls._discard_qrcode(qr_token)
                return {"status": "cancelled", "message": "扫码登录已取消，请重新扫码"}
            try:
                cookies = cls._qrcode_ticket(session, data, channel)
                if not cookies.get("BDUSS") or not cookies.get("STOKEN"):
                    session.get(
                        "https://pan.baidu.com/disk/main",
                        headers=cls._qrcode_headers("https://pan.baidu.com/"),
                        timeout=15,
                    )
                    cookies = cls._baidu_cookies(session)
                if not cookies.get("STOKEN"):
                    session.get(
                        "https://pan.baidu.com/disk/home",
                        headers=cls._qrcode_headers("https://pan.baidu.com/"),
                        timeout=15,
                    )
                    cookies = cls._baidu_cookies(session)
                if not cookies.get("BDUSS"):
                    raise RuntimeError("百度未返回登录 Cookie，请重新扫码或手动填写 Cookie")
                if not cookies.get("STOKEN"):
                    raise RuntimeError("百度未返回 STOKEN，请重试扫码或手动填写完整 Cookie")
                return {
                    "status": "success",
                    "cookie": "; ".join(
                        f"{name}={value}" for name, value in cookies.items()
                    ),
                    "message": "百度网盘登录成功",
                }
            finally:
                cls._discard_qrcode(qr_token)
        except requests.exceptions.Timeout:
            return {"status": "waiting", "message": "等待百度网盘 App 扫码确认"}
        except Exception:
            cls._discard_qrcode(qr_token)
            raise
        finally:
            with cls._qrcode_lock:
                current = cls._qrcode_sessions.get(qr_token)
                if current is item:
                    current["polling"] = False

    @classmethod
    def _qrcode_ticket(
            cls,
            session: requests.Session,
            data: Dict[str, Any],
            channel: Dict[str, Any],
    ) -> Dict[str, str]:
        authsid = str(channel.get("authsid") or data.get("authsid") or "").strip()
        bdusssign = str(channel.get("bdusssign") or channel.get("bduss") or "").strip()
        user_url = str(channel.get("u") or "https://pan.baidu.com/").strip()
        value = str(channel.get("v") or "").strip()
        referer = "https://passport.baidu.com/v3/login/qrcodepage?tpl=netdisk"
        if value.startswith(("http://", "https://")):
            session.get(
                value,
                headers=cls._qrcode_headers(referer),
                allow_redirects=True,
                timeout=15,
            )
        elif authsid and bdusssign:
            session.get(
                f"{cls.PASSPORT_URL}/v3/login/main/qrbdusslogin",
                params={
                    "tt": int(time.time() * 1000),
                    "authsid": authsid,
                    "bduss": bdusssign,
                    "u": user_url,
                    "loginVersion": "v4",
                    "tpl": "netdisk",
                },
                headers=cls._qrcode_headers(referer),
                allow_redirects=True,
                timeout=15,
            )
        elif value:
            session.get(
                f"{cls.PASSPORT_URL}/v3/login/main/qrbdusslogin",
                params={
                    "tt": int(time.time() * 1000),
                    "bduss": value,
                    "loginVersion": "v4",
                    "tpl": "netdisk",
                },
                headers=cls._qrcode_headers(referer),
                allow_redirects=True,
                timeout=15,
            )
        else:
            raise RuntimeError("百度扫码结果缺少换票参数")
        return cls._baidu_cookies(session)

    @staticmethod
    def _baidu_cookies(session: requests.Session) -> Dict[str, str]:
        return {
            cookie.name: cookie.value
            for cookie in session.cookies
            if cookie.domain.lstrip(".") == "baidu.com"
               or cookie.domain.endswith(".baidu.com")
        }
