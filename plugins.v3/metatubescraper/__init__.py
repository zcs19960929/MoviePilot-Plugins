# -*- coding: utf-8 -*-
"""
MetaTube 番号刮削插件（MoviePilot V3）
- 按番号调用自建 MetaTube 服务检索元数据，生成 movie.nfo / poster.jpg / fanart.jpg
- 目录选择器：先选存储（本地 / 115Lite / u115 等），再逐级浏览选择，避免手输错误
- 写入通道：本地存储走文件系统；网盘存储走 MoviePilot StorageChain 上传（受存储能力限制）
- 内置读取/写入限速、单次上限、增量跳过，降低 115 风控风险
"""

import os
import re
import time
import uuid
import random
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from app.plugins import _PluginBase
except Exception:  # MoviePilot V3 新版路径
    from app.sdk.plugin import _PluginBase  # type: ignore

# 日志：V3 插件统一使用宿主 logger（_PluginBase 不再提供 info/error 方法）
try:
    from app.sdk.logging import logger as _logger
except Exception:
    try:
        from app.log import logger as _logger  # type: ignore
    except Exception:
        import logging
        _logger = logging.getLogger("metatubescraper")

# 事件（用于通知 115Lite 生成 STRM）
try:
    from app.sdk.events import eventmanager
except Exception:
    try:
        from app.core.event import eventmanager  # type: ignore
    except Exception:
        eventmanager = None  # type: ignore

try:
    from app.schemas.types import EventType
except Exception:  # 不同版本路径兜底
    try:
        from app.schemas.event import EventType  # type: ignore
    except Exception:
        EventType = None  # type: ignore

# MoviePilot V3 存储链（网盘目录浏览与上传）
try:
    from app.chain.storage import StorageChain as _StorageChain
    from app.schemas.file import FileItem as _FileItem
except Exception:  # 无存储能力的宿主环境降级为仅本地存储
    _StorageChain = None  # type: ignore
    _FileItem = None  # type: ignore


VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts",
    ".rmvb", ".rm", ".mpg", ".mpeg", ".flv", ".iso", ".m4v",
}

def _pick_str(value, default: str = "") -> str:
    """配置值可能是字符串、下拉对象 {title,value} 或列表，统一取字符串"""
    if isinstance(value, dict):
        for key in ("value", "title"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        return default
    if isinstance(value, list):
        for item in value:
            got = _pick_str(item, "")
            if got:
                return got
        return default
    if value is None:
        return default
    return str(value).strip() or default


NOISE_WORDS = {"SD", "HD", "DVD", "BD", "TV", "CD", "RIP", "WEB", "HDR", "DV", "DVDR",
    "MP4", "MKV", "AVI", "WMV", "MOV", "ISO", "EP", "PART", "VOL", "DISC",
    "SUB", "CHS", "CHT", "UHD", "SDR", "DDP", "DTS", "AAC", "FLAC", "HEVC",
    "AV1", "IMAX", "EXT", "FIN", "LEAK", "UNCUT", "CENSORED", "UNCENSORED",
    "NEW", "HQ", "GB", "MB", "TB", "ENG", "RAW", "CAM",
}

# ---- 表单目录浏览器的事件脚本（MoviePilot V3 FormRender 支持 on* 事件字符串）----
JS_LOAD = """async () => {
  dir_loading = true; dir_error = '';
  try {
    const st = (storage && storage.value) ? storage.value : (storage || 'local');
    const p = (browse_path || '/').trim() || '/';
    const r = await window.MoviePilotAPI.post('/storage/list', { storage: st, path: p, type: 'dir' });
    const arr = Array.isArray(r) ? r : ((r && r.data) || []);
    dir_items = arr.filter(x => x && x.type === 'dir').map(x => ({ title: x.name, value: x.path }));
    if (!dir_items.length) { dir_error = '该目录下没有子目录'; }
  } catch (e) {
    dir_error = '读取失败：' + ((e && e.message) ? e.message : e);
  } finally { dir_loading = false; }
}"""

JS_ENTER = """async (v) => {
  const path = (v && v.value) ? v.value : v;
  if (!path) return;
  browse_path = path; pick_dir = null;
  dir_loading = true; dir_error = '';
  try {
    const st = (storage && storage.value) ? storage.value : (storage || 'local');
    const r = await window.MoviePilotAPI.post('/storage/list', { storage: st, path: path, type: 'dir' });
    const arr = Array.isArray(r) ? r : ((r && r.data) || []);
    dir_items = arr.filter(x => x && x.type === 'dir').map(x => ({ title: x.name, value: x.path }));
    if (!dir_items.length) { dir_error = '该目录下没有子目录'; }
  } catch (e) {
    dir_error = '读取失败：' + ((e && e.message) ? e.message : e);
  } finally { dir_loading = false; }
}"""

JS_UP = """async () => {
  const p = String(browse_path || '/').replace(/\\/+$/, '');
  const i = p.lastIndexOf('/');
  browse_path = i <= 0 ? '/' : p.slice(0, i);
  pick_dir = null;
  dir_loading = true; dir_error = '';
  try {
    const st = (storage && storage.value) ? storage.value : (storage || 'local');
    const r = await window.MoviePilotAPI.post('/storage/list', { storage: st, path: browse_path, type: 'dir' });
    const arr = Array.isArray(r) ? r : ((r && r.data) || []);
    dir_items = arr.filter(x => x && x.type === 'dir').map(x => ({ title: x.name, value: x.path }));
  } catch (e) {
    dir_error = '读取失败：' + ((e && e.message) ? e.message : e);
  } finally { dir_loading = false; }
}"""

JS_SELFTEST = """async () => {
  run_msg = '测试中…';
  const n = (test_number || 'SSIS-110');
  const paths = ['/plugin/MetaTubeScraper/selftest?q=', '/plugin/metatubescraper/selftest?q='];
  for (const base of paths) {
    try {
      const r = await window.MoviePilotAPI.get(base + encodeURIComponent(n));
      const d = (r && r.data) ? r.data : r;
      run_msg = (d && d.msg) ? d.msg : JSON.stringify(d);
      return;
    } catch (e) {
      run_msg = '测试失败：' + ((e && e.message) ? e.message : e);
    }
  }
}"""

JS_RUN = """async () => {
  run_msg = '正在触发…';
  const paths = ['/plugin/MetaTubeScraper/run', '/plugin/metatubescraper/run'];
  for (const p of paths) {
    try {
      const r = await window.MoviePilotAPI.get(p);
      const d = (r && r.data) ? r.data : r;
      run_msg = (d && d.msg) ? d.msg : JSON.stringify(d);
      return;
    } catch (e) {
      run_msg = '触发失败：' + ((e && e.message) ? e.message : e);
    }
  }
}"""

JS_ADD = """() => {
  const p = (browse_path || '').trim();
  if (!p) { dir_error = '请先输入或选择路径'; return; }
  const cur = String(scan_dirs || '').split(/[\\n,;]+/).map(s => s.trim()).filter(Boolean);
  if (cur.indexOf(p) === -1) { cur.push(p); }
  scan_dirs = cur.join('\\n');
  dir_error = '已添加：' + p;
}"""


class MetaTubeScraper(_PluginBase):
    plugin_name = "MetaTube 番号刮削"
    plugin_desc = (
        "按番号调用自建 MetaTube 服务刮削元数据，生成 movie.nfo、poster.jpg、fanart.jpg 并写入所选存储目录。"
        "支持本地与网盘存储（115Lite/u115 等），目录可视化选择，内置限速与单次上限以降低 115 风控风险。"
    )
    plugin_icon = "https://github.com/metatube-community.png"
    plugin_version = "1.0.0"
    plugin_config_prefix = "metatubescraper_"
    plugin_order = 20
    auth_level = 1

    # ---- 日志兼容层：V2 基类有 info/warn/error，V3 只有 logger ----
    def info(self, msg: str, *args, **kwargs) -> None:
        _logger.info(f"【MetaTube刮削】{msg}")

    def warn(self, msg: str, *args, **kwargs) -> None:
        _logger.warning(f"【MetaTube刮削】{msg}")

    def error(self, msg: str, *args, **kwargs) -> None:
        _logger.error(f"【MetaTube刮削】{msg}")

    def debug(self, msg: str, *args, **kwargs) -> None:
        _logger.debug(f"【MetaTube刮削】{msg}")

    _running = False
    _lock = threading.Lock()
    _stop_event = threading.Event()
    _upload_unsupported = False
    _last_status = None
    _search_notes: List[str] = []

    _enabled = False
    _url = ""
    _token = ""
    _providers = ""
    _storage = "local"
    _scan_dirs: List[str] = []
    _scan_mode = "subdir"
    _file_interval = 3
    _movie_limit = 30
    _cron_hour = 4
    _cron_minute = 30
    _force = False
    _download_previews = False
    _preview_limit = 10
    _rename_files = False
    _numeric_ids = False
    # v1.2 输出位置与归位
    _write_local = True
    _write_remote = False
    _local_strm_root = ""
    _remote_lib_root = ""
    _organize = False
    _trigger_strm = True

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._url = _pick_str(config.get("metatube_url"))
        self._token = _pick_str(config.get("metatube_token"))
        self._providers = _pick_str(config.get("providers"))
        self._storage = _pick_str(config.get("storage"), "local")
        dirs = config.get("scan_dirs") or ""
        if isinstance(dirs, list):
            dirs = "\n".join(_pick_str(d) for d in dirs)
        self._scan_dirs = [d.strip() for d in re.split(r"[,;\n\r]+", str(dirs)) if d.strip()]
        self._scan_mode = config.get("scan_mode") or "subdir"
        for key, default, attr in (
            ("file_interval", 3, "_file_interval"),
            ("movie_limit", 30, "_movie_limit"),
            ("cron_hour", 4, "_cron_hour"),
            ("cron_minute", 30, "_cron_minute"),
            ("preview_limit", 10, "_preview_limit"),
        ):
            try:
                setattr(self, attr, int(float(config.get(key) or default)))
            except (TypeError, ValueError):
                setattr(self, attr, default)
        self._file_interval = max(1, self._file_interval)
        self._movie_limit = max(1, self._movie_limit)
        self._cron_hour = min(23, max(0, self._cron_hour))
        self._cron_minute = min(59, max(0, self._cron_minute))
        self._preview_limit = max(0, self._preview_limit)
        self._force = bool(config.get("force_overwrite"))
        self._download_previews = bool(config.get("download_previews"))
        self._rename_files = bool(config.get("rename_files"))
        self._numeric_ids = bool(config.get("numeric_ids"))
        self._write_local = bool(config.get("write_local", True))
        self._write_remote = bool(config.get("write_remote"))
        self._local_strm_root = _pick_str(config.get("local_strm_root")).rstrip("/")
        self._remote_lib_root = _pick_str(config.get("remote_lib_root")).rstrip("/")
        self._organize = bool(config.get("organize"))
        self._trigger_strm = bool(config.get("trigger_strm", True))
        self._upload_unsupported = False

    @property
    def _config_ok(self) -> bool:
        return bool(self._enabled and self._url and self._scan_dirs)

    @property
    def _is_local(self) -> bool:
        return self._storage in ("", "local")

    def get_state(self) -> bool:
        return self._config_ok

    # ---------------- 番号识别 ----------------

    @staticmethod
    def __extract_number(text: str, numeric_ids: bool = False) -> Optional[str]:
        if not text:
            return None
        t = re.sub(r"[\s_.]+", "-", text.upper())

        m = re.search(r"(?<![A-Z0-9])FC2(?:-PPV)?-(\d{6,9})(?!\d)", t)
        if m:
            return f"FC2-PPV-{m.group(1)}"

        m = re.search(r"(?<![A-Z0-9])(\d{2,3}[A-Z]{2,6})-(\d{2,5})(?!\d)", t)
        if m:
            return f"{m.group(1)}-{m.group(2)}"

        for m in re.finditer(r"(?<![A-Z0-9])([A-Z]{2,6})-?(\d{2,5})(?!\d)(?![PIK])", t):
            prefix = m.group(1)
            if prefix in NOISE_WORDS:
                continue
            digits = m.group(2)
            if len(digits) == 2:
                digits = "0" + digits
            return f"{prefix}-{digits}"

        if numeric_ids:
            for pattern in (r"(?<!\d)(\d{6})-(\d{3})(?!\d)", r"(?<!\d)(\d{8})-(\d{3})(?!\d)"):
                m = re.search(pattern, t)
                if m:
                    return f"{m.group(1)}-{m.group(2)}"
        return None

    # ---------------- MetaTube 客户端 ----------------

    def __mt_get(self, path: str, params: dict = None,
                 stream: bool = False, timeout: int = 20):
        url = f"{self._url.rstrip('/')}{path}"
        headers = {"User-Agent": "MoviePilot-MetaTubeScraper/1.1"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        for attempt in range(2):
            try:
                resp = requests.get(url, params=params, headers=headers,
                                    timeout=timeout, stream=stream)
                if resp.status_code == 200:
                    return resp if stream else resp.json()
                self._last_status = resp.status_code
                self.debug(f"MetaTube 响应异常 {resp.status_code}: {url}")
                return None
            except Exception as e:
                if attempt == 0:
                    time.sleep(1)
                else:
                    self.error(f"请求 MetaTube 失败: {url} - {e}")
                    return None

    def __search_movie(self, number: str) -> Tuple[Optional[dict], Optional[str]]:
        """按番号检索：配置的源依次尝试 → 去横线变体重试 → 全局检索兜底"""
        candidates = [number]
        alt = number.replace("-", "")
        if alt and alt != number:
            candidates.append(alt)
        providers = [p.strip() for p in self._providers.split(",") if p.strip()]
        self._search_notes = []
        for cand in candidates:
            if providers:
                for p in providers:
                    data = self.__mt_get("/v1/movies/search",
                                         {"q": cand, "provider": p, "fallback": "true"})
                    results = (data or {}).get("data") or []
                    if results:
                        return results[0], p
                    self._search_notes.append(f"{p}:{self._last_status or '空'}")
            # 全局兜底：不指定 provider，走 MetaTube 数据库缓存 + 全部可用源
            data = self.__mt_get("/v1/movies/search", {"q": cand, "fallback": "true"})
            results = (data or {}).get("data") or []
            if results:
                return results[0], results[0].get("provider")
            self._search_notes.append(f"全局:{self._last_status or '空'}")
        return None, None

    def __get_movie_info(self, provider: str, movie_id: str) -> Optional[dict]:
        data = self.__mt_get(f"/v1/movies/{provider}/{movie_id}", {"lazy": "true"})
        if isinstance(data, dict):
            return data.get("data")
        return None

    def __download_image_bytes(self, path: str) -> Optional[bytes]:
        resp = self.__mt_get(path, stream=True, timeout=60)
        if resp is None:
            return None
        try:
            if not resp.headers.get("content-type", "").startswith("image"):
                return None
            return resp.content
        except Exception as e:
            self.debug(f"下载图片失败 {path}: {e}")
            return None
        finally:
            resp.close()

    def __download_url_bytes(self, url: str, referer: str) -> Optional[bytes]:
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": referer or "",
            }, timeout=60)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                return resp.content
        except Exception as e:
            self.debug(f"下载剧照失败: {url} - {e}")
        return None

    # ---------------- NFO ----------------

    def __build_nfo(self, info: dict, number: str) -> bytes:
        movie = ET.Element("movie")

        def sub(tag: str, text) -> None:
            if text is None or str(text).strip() == "":
                return
            ET.SubElement(movie, tag).text = str(text).strip()

        title = info.get("title") or number
        sub("title", title)
        sub("originaltitle", title)
        sub("num", number)
        sub("plot", info.get("summary"))
        release_date = str(info.get("release_date") or "")[:10]
        sub("premiered", release_date)
        sub("releasedate", release_date)
        if release_date and release_date[:4].isdigit():
            sub("year", release_date[:4])
        if info.get("runtime"):
            sub("runtime", info.get("runtime"))
        sub("director", info.get("director"))
        sub("studio", info.get("maker"))
        sub("label", info.get("label"))
        sub("set", info.get("series"))
        sub("mpaa", "NR")
        for genre in (info.get("genres") or []):
            sub("genre", genre)
        for actor in (info.get("actors") or []):
            actor_el = ET.SubElement(movie, "actor")
            ET.SubElement(actor_el, "name").text = str(actor).strip()
            ET.SubElement(actor_el, "type").text = "Actor"
        if info.get("score"):
            ratings = ET.SubElement(movie, "ratings")
            rating = ET.SubElement(ratings, "rating")
            rating.set("name", "metatube")
            rating.set("max", "10")
            rating.set("default", "true")
            rating.set("value", str(info.get("score")))
        provider = info.get("provider") or ""
        movie_id = info.get("id") or ""
        if provider and movie_id:
            uid = ET.SubElement(movie, "uniqueid")
            uid.set("type", "metatube")
            uid.set("default", "true")
            uid.text = f"{provider}:{movie_id}"
        if info.get("preview_video_url"):
            sub("trailer", info.get("preview_video_url"))
        if self._url and provider and movie_id:
            base = self._url.rstrip("/")
            art = ET.SubElement(movie, "art")
            ET.SubElement(art, "poster").text = f"{base}/v1/images/primary/{provider}/{movie_id}"
            ET.SubElement(art, "fanart").text = f"{base}/v1/images/backdrop/{provider}/{movie_id}"
        ET.indent(movie, space="  ")
        return ET.tostring(movie, encoding="UTF-8", xml_declaration=True)

    # ---------------- 存储抽象层 ----------------

    def __throttle(self):
        self._stop_event.wait(self._file_interval + random.uniform(0, 1.5))

    def __chain(self):
        if _StorageChain is None or _FileItem is None:
            self.error("当前宿主不存在 StorageChain，无法访问网盘存储")
            return None
        return _StorageChain()

    def __make_item(self, path: str, item_type: str = "dir"):
        return _FileItem(storage=self._storage, path=str(path), type=item_type)

    def list_children(self, path: str) -> List[dict]:
        """列出目录下条目：[{name, path, type}]，path 为存储内路径"""
        path = str(path or "/")
        if self._is_local:
            out, base = [], Path(path)
            try:
                for entry in os.scandir(base):
                    out.append({
                        "name": entry.name,
                        "path": str(base / entry.name),
                        "type": "dir" if entry.is_dir() else "file",
                    })
            except OSError as e:
                self.warn(f"读取本地目录失败 {path}: {e}")
            return out
        chain = self.__chain()
        if not chain:
            return []
        try:
            files = chain.list_files(self.__make_item(path)) or []
            out = []
            for f in files:
                name = f.name or Path(f.path or "").name
                if not name:
                    continue
                out.append({
                    "name": name,
                    "path": str(Path(path) / name),
                    "type": "dir" if f.type == "dir" else "file",
                })
            return out
        except Exception as e:
            self.error(f"读取网盘目录失败 {path}: {e}")
            return []

    def __write_local_file(self, dir_path: str, filename: str, data: bytes) -> bool:
        """写本地文件（容器内路径，零网盘 API 调用）"""
        self.__throttle()
        if self._stop_event.is_set():
            return False
        target = Path(dir_path) / filename
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return True
        except Exception as e:
            self.error(f"写入本地文件失败 {target}: {e}")
            return False

    def __upload_bytes(self, dir_path: str, filename: str, data: bytes) -> bool:
        """写网盘文件（StorageChain 上传）"""
        self.__throttle()
        if self._stop_event.is_set():
            return False
        if self._upload_unsupported:
            return False
        chain = self.__chain()
        if not chain:
            return False
        tmp = Path(tempfile.gettempdir()) / f"mts_{uuid.uuid4().hex}_{filename}"
        try:
            tmp.write_bytes(data)
            dir_item = chain.get_file_item(self._storage, Path(dir_path))
            if dir_item is None:
                self.warn(f"目标目录不存在或不可访问: {dir_path}")
                return False
            result = chain.upload_file(dir_item, tmp, new_name=filename)
            if result is None:
                self._upload_unsupported = True
                self.error(
                    f"存储「{self._storage}」不支持上传（115Lite 仅支持浏览/移动/重命名/删除）。"
                    f"请改用 MP 原生 115 存储（u115）或本地挂载路径写入。"
                )
                return False
            return True
        except Exception as e:
            self.error(f"上传文件失败 {dir_path}/{filename}: {e}")
            return False
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def __put_file(self, dir_path: str, filename: str, data: bytes) -> bool:
        """按当前存储类型写入文件"""
        if self._is_local:
            return self.__write_local_file(dir_path, filename, data)
        return self.__upload_bytes(dir_path, filename, data)

    # ---- v1.2 归位与本地 STRM 目录映射 ----

    def __under_lib(self, remote_dir: str) -> bool:
        if not self._remote_lib_root:
            return False
        d = (remote_dir or "/").rstrip("/") or "/"
        root = self._remote_lib_root
        return d == root or d.startswith(root + "/")

    def __map_local(self, remote_dir: str, number: str) -> Optional[str]:
        """把网盘路径映射为本地 STRM 目录下的对应路径"""
        if not self._local_strm_root:
            return None
        rd = (remote_dir or "/").rstrip("/") or "/"
        root = self._remote_lib_root
        if root and rd == root:
            rel = ""
        elif root and rd.startswith(root + "/"):
            rel = rd[len(root) + 1:]
        else:
            rel = number
        if not rel or ".." in rel.split("/"):
            rel = number
        return str(Path(self._local_strm_root) / rel)

    def __organize_move(self, src_dir: str, videos: List[dict],
                        number: str) -> Optional[str]:
        """把影片从资源库移动到媒体库目标目录（需要存储实现 move，如 u115）"""
        if self._is_local or not self._remote_lib_root:
            return None
        chain = self.__chain()
        if not chain:
            return None
        target = str(Path(self._remote_lib_root) / number)
        try:
            dir_item = chain.get_folder(self._storage, Path(target))
        except Exception as e:
            self.error(f"创建媒体库目录失败 {target}: {e}")
            return None
        if dir_item is None:
            self.error(f"媒体库目录不可用: {target}")
            return None
        moved = 0
        for video in videos:
            self.__throttle()
            if self._stop_event.is_set():
                break
            name = video["name"]
            ext = Path(name).suffix
            try:
                item = chain.get_file_item(self._storage, Path(src_dir) / name)
                if item is None:
                    continue
                res = chain.run_module("move", fileitem=item,
                                       path=Path(target), new_name=f"{number}{ext}")
                if res:
                    moved += 1
                    self.info(f"[{number}] 已归位 {name} -> {target}")
                else:
                    self.warn(f"[{number}] 归位失败 {name}（该存储可能不支持 move；115Lite 需改用 u115 或手工归位）")
            except Exception as e:
                self.warn(f"[{number}] 归位异常 {name}: {e}")
        return target if moved else None

    def __rename_video(self, dir_path: str, old_name: str, new_name: str) -> bool:
        self.__throttle()
        try:
            if self._is_local:
                os.rename(Path(dir_path) / old_name, Path(dir_path) / new_name)
                return True
            chain = self.__chain()
            item = chain.get_file_item(self._storage, Path(dir_path) / old_name) if chain else None
            if item is None:
                return False
            return bool(chain.rename_file(item, new_name))
        except Exception as e:
            self.warn(f"重命名失败 {old_name}: {e}")
            return False

    # ---------------- 扫描 ----------------

    def __scan_targets(self) -> List[str]:
        targets: List[str] = []
        for d in self._scan_dirs:
            if self._is_local:
                if not os.path.isdir(d):
                    self.warn(f"目录不存在或不可访问: {d}")
                    continue
                if self._scan_mode == "recursive":
                    for root, subdirs, files in os.walk(d):
                        if any(f.lower().endswith(tuple(VIDEO_EXTS)) for f in files):
                            targets.append(root)
                            subdirs[:] = []
                else:
                    try:
                        for entry in os.scandir(d):
                            if entry.is_dir():
                                targets.append(entry.path)
                    except OSError as e:
                        self.warn(f"读取目录失败 {d}: {e}")
            else:
                if self._scan_mode == "recursive":
                    targets.extend(self.__walk_remote(d, max_depth=3))
                else:
                    for child in self.list_children(d):
                        if child["type"] == "dir":
                            targets.append(child["path"])
        return targets

    def __walk_remote(self, path: str, max_depth: int = 3) -> List[str]:
        """网盘递归扫描（限制层数，避免大量 API 调用触发风控）"""
        results: List[str] = []
        queue = [(path, 0)]
        while queue:
            current, depth = queue.pop(0)
            if self._stop_event.is_set() or depth > max_depth:
                continue
            children = self.list_children(current)
            videos = [c for c in children if c["type"] == "file"
                      and Path(c["name"]).suffix.lower() in VIDEO_EXTS]
            if videos:
                results.append(current)
                continue
            for c in children:
                if c["type"] == "dir":
                    queue.append((c["path"], depth + 1))
        return results

    # ---------------- 单目录处理 ----------------

    def __trigger_strm(self, remote_dir: str) -> None:
        """通知 115Lite 为指定网盘目录生成 STRM（等价于消息命令 /115strm <目录>）"""
        if not self._trigger_strm or not remote_dir:
            return
        if eventmanager is None or EventType is None:
            return
        try:
            eventmanager.send_event(EventType.PluginAction, {
                "action": "p115lite_strm",
                "arg_str": remote_dir,
            })
            self.info(f"已通知 115Lite 为 {remote_dir} 生成 STRM")
        except Exception as e:
            self.debug(f"触发 115Lite STRM 生成失败: {e}")

    def __process_target(self, dir_path: str) -> str:
        try:
            children = self.list_children(dir_path)
            if not children:
                return "skip"
            names = {c["name"].lower() for c in children}
            if "movie.nfo" in names and not self._force:
                return "skip"
            videos = [c for c in children if c["type"] == "file"
                      and Path(c["name"]).suffix.lower() in VIDEO_EXTS]
            if not videos:
                return "skip"

            video_name = videos[0]["name"]
            number = (self.__extract_number(video_name, self._numeric_ids)
                      or self.__extract_number(Path(dir_path).name, self._numeric_ids))
            if not number:
                self.debug(f"未识别到番号: {dir_path} / {video_name}")
                return "nomatch"

            result, provider = self.__search_movie(number)
            if not result:
                notes = " ".join(getattr(self, "_search_notes", None) or [])
                self.warn(f"{number} 未在 MetaTube 检索到结果 ({dir_path})｜各源返回：{notes}")
                return "notfound"
            info = self.__get_movie_info(provider, result.get("id") or "")
            if not info or not info.get("title"):
                self.warn(f"{number} 元数据获取失败 (provider={provider})")
                return "notfound"

            provider = info.get("provider") or provider
            movie_id = info.get("id") or result.get("id")

            # 归位：资源库 -> 媒体库（可选，需要存储支持 move，如 u115）
            final_remote = dir_path
            if self._organize and not self._is_local and not self.__under_lib(dir_path):
                moved_to = self.__organize_move(dir_path, videos, number)
                if not moved_to:
                    self.warn(f"[{number}] 归位未完成，本轮跳过（文件仍在原目录）")
                    return "fail"
                final_remote = moved_to
                names = set()

            # 输出位置：本地 STRM 目录 / 网盘目录
            local_dir = None
            if self._is_local:
                local_dir = final_remote
            elif self._write_local:
                local_dir = self.__map_local(final_remote, number)
            remote_write = (not self._is_local) and self._write_remote
            # 源目录不在媒体库根目录下时，本地映射路径不可信：只允许写网盘，避免本地产生空目录
            if local_dir and not self._is_local and not self.__under_lib(final_remote):
                if not remote_write:
                    self.warn(
                        f"[{number}] 影片不在媒体库根目录（{final_remote}）下，且未开启自动归位；"
                        f"为避免生成本地空目录，本轮跳过。请开启「自动归位」或先移动影片后再跑。"
                    )
                    return "skip"
                local_dir = None
            if not local_dir and not remote_write:
                self.warn("未启用任何输出位置（本地 STRM 目录 / 网盘目录），请检查配置")
                return "fail"

            wrote = False

            def emit(filename: str, data: bytes) -> bool:
                nonlocal wrote
                done = False
                if local_dir and self.__write_local_file(local_dir, filename, data):
                    done = True
                if remote_write and self.__upload_bytes(final_remote, filename, data):
                    done = True
                if done:
                    wrote = True
                return done

            if emit("movie.nfo", self.__build_nfo(info, number)):
                self.info(f"[{number}] 已写入 movie.nfo -> {local_dir or final_remote}")

            if self._force or "poster.jpg" not in names:
                data = self.__download_image_bytes(f"/v1/images/primary/{provider}/{movie_id}")
                if data and emit("poster.jpg", data):
                    self.info(f"[{number}] 已写入 poster.jpg")

            if self._force or "fanart.jpg" not in names:
                data = self.__download_image_bytes(f"/v1/images/backdrop/{provider}/{movie_id}")
                if data and emit("fanart.jpg", data):
                    self.info(f"[{number}] 已写入 fanart.jpg")

            if self._download_previews and self._preview_limit > 0:
                previews = list(info.get("preview_images") or [])[: self._preview_limit]
                referer = info.get("homepage") or ""
                count = 0
                for i, url in enumerate(previews, start=1):
                    if self._stop_event.is_set():
                        break
                    data = self.__download_url_bytes(url, referer)
                    if data and emit(f"extrafanart{i}.jpg", data):
                        count += 1
                if count:
                    self.info(f"[{number}] 已写入剧照 {count} 张")

            if self._rename_files and final_remote == dir_path:
                for video in videos:
                    name = video["name"]
                    ext = Path(name).suffix
                    if number.upper() in name.upper():
                        continue
                    target_name = f"{number}{ext}"
                    if target_name.lower() in names:
                        continue
                    if self.__rename_video(dir_path, name, target_name):
                        self.info(f"[{number}] 已重命名 {name} -> {target_name}")

            if wrote and not self._is_local:
                self.__trigger_strm(final_remote)

            return "ok" if wrote else "fail"
        except Exception as e:
            self.error(f"处理目录失败 {dir_path}: {e}")
            return "fail"
    # ---------------- 任务 ----------------

    def __scrape_all(self):
        with self._lock:
            if self._running:
                self.warn("上一次刮削任务尚未结束，本次跳过")
                return
            self._running = True
        self._stop_event.clear()
        stats = {"total": 0, "ok": 0, "skip": 0, "nomatch": 0,
                 "notfound": 0, "fail": 0, "time": "", "storage": self._storage}
        try:
            self.info(f"MetaTube 番号刮削开始，存储: {self._storage}，目录: {self._scan_dirs}")
            if not self._is_local:
                self.info("网盘存储模式：目录遍历与写入通过 StorageChain，已按文件写入间隔限速")
            targets = self.__scan_targets()
            stats["total"] = len(targets)
            self.info(f"共发现 {len(targets)} 个待处理目录")
            for t in targets:
                if self._stop_event.is_set():
                    self.warn("任务被手动停止")
                    break
                handled = stats["ok"] + stats["notfound"] + stats["fail"]
                if handled >= self._movie_limit:
                    self.info(f"已达到单次处理上限 {self._movie_limit} 部，剩余目录下次执行")
                    break
                status = self.__process_target(t)
                stats[status] = stats.get(status, 0) + 1
        except Exception as e:
            self.error(f"刮削任务异常: {e}")
        finally:
            self._running = False
            from datetime import datetime
            stats["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.save_data("last_run", stats)
            self.info(
                f"MetaTube 番号刮削完成: 共 {stats['total']} 个目录, "
                f"成功 {stats['ok']}, 跳过 {stats['skip']}, "
                f"未识别 {stats['nomatch']}, 未找到 {stats['notfound']}, 失败 {stats['fail']}"
            )

    def __run_scheduled(self):
        if self._config_ok:
            self.__scrape_all()

    # ---------------- 插件契约 ----------------

    def stop_service(self):
        self._stop_event.set()

    def get_service(self) -> List[Dict[str, Any]]:
        if self._config_ok:
            return [{
                "id": "MetaTubeScraper",
                "name": "MetaTube 番号刮削",
                "trigger": "cron",
                "func": self.__run_scheduled,
                "kwargs": {"hour": self._cron_hour, "minute": self._cron_minute},
            }]
        return []

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.__api_run,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "立即执行刮削",
            "description": "手动触发一次 MetaTube 番号刮削任务",
        }, {
            "path": "/selftest",
            "endpoint": self.__api_selftest,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "测试数据源",
            "description": "用指定番号逐个测试已配置的数据源，返回每个源的检索结果",
        }]

    def __api_selftest(self, q: str = "SSIS-110") -> Dict[str, Any]:
        """逐源探测：区分 provider 不存在 / 搜不到 / 有结果"""
        number = (q or "SSIS-110").strip()
        if not self._url:
            return {"code": 400, "msg": "请先填写 MetaTube 服务地址"}
        providers = [p.strip() for p in self._providers.split(",") if p.strip()] or [""]
        lines = []
        for p in providers:
            params = {"q": number, "fallback": "true"}
            if p:
                params["provider"] = p
            label = p or "全局"
            try:
                resp = requests.get(f"{self._url.rstrip('/')}/v1/movies/search",
                                    params=params, timeout=20)
                if resp.status_code == 200:
                    data = (resp.json() or {}).get("data") or []
                    if data:
                        title = str(data[0].get("title") or "")[:24]
                        lines.append(f"{label}: 命中「{title}」")
                    else:
                        lines.append(f"{label}: 空结果")
                else:
                    msg = ""
                    try:
                        msg = (resp.json() or {}).get("error", {}).get("message", "")
                    except Exception:
                        pass
                    lines.append(f"{label}: HTTP {resp.status_code} {msg}")
            except Exception as e:
                lines.append(f"{label}: 请求异常 {e}")
        return {"code": 0, "msg": f"{number} → " + " | ".join(lines)}

    def __api_run(self) -> Dict[str, Any]:
        if not self._config_ok:
            return {"code": 400, "msg": "插件未启用，或服务地址/媒体目录未配置"}
        if self._running:
            return {"code": 400, "msg": "刮削任务正在运行中，请勿重复触发"}
        threading.Thread(target=self.__scrape_all, daemon=True).start()
        return {"code": 0, "msg": "刮削任务已开始，可在插件详情页或系统日志中查看进度"}

    def get_page(self) -> List[dict]:
        stats = self.get_data("last_run")
        if not stats:
            return [{
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "暂无运行记录，配置完成后可通过「立即执行刮削」按钮触发一次任务。",
                },
            }]
        text = (f"运行时间: {stats.get('time', '-')} | 存储: {stats.get('storage', '-')} | "
                f"扫描目录: {stats.get('total', 0)} | 成功: {stats.get('ok', 0)} | "
                f"跳过: {stats.get('skip', 0)} | 未识别: {stats.get('nomatch', 0)} | "
                f"未找到: {stats.get('notfound', 0)} | 失败: {stats.get('fail', 0)}")
        return [{
            "component": "VAlert",
            "props": {
                "type": "success" if stats.get("fail", 0) == 0 else "warning",
                "variant": "tonal",
                "text": text,
            },
        }]

    # ---------------- 配置表单 ----------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:

        def row(*cols) -> dict:
            return {"component": "VRow", "content": list(cols)}

        def col(*items, cols=12, md=6) -> dict:
            return {"component": "VCol", "props": {"cols": cols, "md": md}, "content": list(items)}

        def text(model: str, label: str, placeholder: str = "", md: int = 6, tip: str = "") -> dict:
            props = {"model": model, "label": label, "placeholder": placeholder}
            if tip:
                props["hint"] = tip
                props["persistent-hint"] = True
            return col({"component": "VTextField", "props": props}, md=md)

        def switch(model: str, label: str, md: int = 6, tip: str = "") -> dict:
            props = {"model": model, "label": label}
            if tip:
                props["hint"] = tip
                props["persistent-hint"] = True
            return col({"component": "VSwitch", "props": props}, md=md)

        form = [{
            "component": "VForm",
            "content": [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "先选存储类型，再用目录浏览器逐级进入目标目录，最后点「添加为媒体目录」。"
                                "网盘存储的浏览与写入通过 MoviePilot 存储接口完成，已按写入间隔限速以降低 115 风控风险。",
                    },
                },
                row(
                    text("metatube_url", "MetaTube 服务地址", "http://192.168.31.60:8090",
                         tip="官方镜像端口映射 8090 -> 8080；未启用 token 时留空"),
                    text("metatube_token", "Token（可选）", "留空即可",
                         tip="仅当服务端启动参数带 -token 时填写"),
                ),
                row(
                    text("providers", "数据源优先级", "FANZA,JavBus,MGS,FC2PPVDB,fc2hub",
                         md=12,
                         tip="ID 必须与服务端 /v1/providers 返回完全一致（区分大小写）。"
                             "实测 FANZA 最稳定；FC2PPVDB/fc2hub 仅对 FC2 番号有效"),
                ),
                {
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "mt-2 mb-2"},
                    "content": [{
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {"type": "warning", "variant": "tonal", "density": "compact",
                                          "class": "mb-3",
                                          "text": "115Lite 存储不支持上传，只能浏览定位；写入请选「本地存储」"
                                                  "（rclone/CloudDrive2 挂载路径）或「MP 原生 115（u115）」。",
                                          },
                            },
                            row(
                                col({
                                    "component": "VCombobox",
                                    "props": {
                                        "model": "storage",
                                        "label": "存储类型",
                                        "itemTitle": "title",
                                        "itemValue": "value",
                                        "returnObject": False,
                                        "items": [
                                            {"title": "本地存储（local）", "value": "local"},
                                            {"title": "115Lite 网盘（仅浏览）", "value": "115Lite"},
                                            {"title": "MP 原生 115（u115，可上传）", "value": "u115"},
                                            {"title": "其他存储（可手填存储名）", "value": "alist"},
                                        ],
                                    },
                                }, cols=12, md=5),
                                col({
                                    "component": "VTextField",
                                    "props": {
                                        "model": "browse_path",
                                        "label": "当前浏览路径",
                                        "placeholder": "/jav",
                                    },
                                }, cols=12, md=7),
                            ),
                            row(
                                col({
                                    "component": "VBtn",
                                    "props": {"size": "small", "variant": "tonal",
                                              "prepend-icon": "mdi-folder-refresh", "onClick": JS_LOAD},
                                    "text": "读取目录",
                                }, cols=6, md=2),
                                col({
                                    "component": "VBtn",
                                    "props": {"size": "small", "variant": "tonal",
                                              "prepend-icon": "mdi-arrow-up-bold", "onClick": JS_UP},
                                    "text": "上一级",
                                }, cols=6, md=2),
                                col({
                                    "component": "VBtn",
                                    "props": {"size": "small", "color": "primary", "variant": "flat",
                                              "prepend-icon": "mdi-plus", "onClick": JS_ADD},
                                    "text": "添加为媒体目录",
                                }, cols=12, md=3),
                            ),
                            row(
                                col({
                                    "component": "VSelect",
                                    "props": {
                                        "model": "pick_dir",
                                        "label": "子目录（选择后自动进入）",
                                        "items": "{{ dir_items }}",
                                        "itemTitle": "title",
                                        "itemValue": "value",
                                        "returnObject": False,
                                        "clearable": True,
                                        "onUpdate:modelValue": JS_ENTER,
                                    },
                                }, cols=12, md=8),
                                col({
                                    "component": "VTextField",
                                    "props": {"model": "dir_error", "label": "浏览状态",
                                              "readonly": True, "variant": "plain"},
                                }, cols=12, md=4),
                            ),
                        ],
                    }],
                },
                row(
                    col({
                        "component": "VTextarea",
                        "props": {
                            "model": "scan_dirs",
                            "label": "媒体目录（每行一个；本地填容器内绝对路径，网盘填网盘内路径如 /jav）",
                            "rows": 3,
                        },
                    }, cols=12),
                ),
                {
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "mb-2"},
                    "content": [{
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info", "variant": "tonal", "density": "compact",
                                    "class": "mb-3",
                                    "text": "输出位置：网盘媒体库根目录与本地 STRM 目录一一对应，"
                                            "网盘 /jav/SSIS-110 对应本地 {STRM根}/SSIS-110。"
                                            "Emby/Jellyfin 读本地 NFO 即可；爆米花可直连 Emby 获得海报墙。",
                                },
                            },
                            row(
                                switch("write_local", "写入本地 STRM 目录（推荐）",
                                       tip="与 .strm 同级写 NFO/海报，零网盘写入风险"),
                                switch("write_remote", "写入网盘目录",
                                       tip="需存储支持上传（u115/alist）；115Lite 不支持上传"),
                            ),
                            row(
                                text("remote_lib_root", "网盘媒体库根目录", "/jav",
                                     tip="115Lite 里的媒体库目录，如 /jav"),
                                text("local_strm_root", "本地 STRM 根目录", "/media/strm/jav",
                                     tip="115Lite 配置中「本地 STRM 目录」对应的容器内路径"),
                            ),
                            row(
                                switch("organize", "自动归位（资源库 → 媒体库）",
                                       tip="把番号影片移动到 媒体库根目录/番号 下；"
                                           "需要存储实现 move（u115 支持，115Lite 不支持）"),
                                switch("trigger_strm", "刮完通知 115Lite 生成 STRM",
                                       tip="等价于对该目录执行 /115strm；需 115Lite 里已配置"
                                           "「本地STRM目录#网盘目录」映射"),
                            ),
                        ],
                    }],
                },
                row(
                    col({
                        "component": "VSelect",
                        "props": {
                            "model": "scan_mode",
                            "label": "扫描模式",
                            "items": [
                                {"title": "仅扫描一级子目录（推荐，目录请求最少）", "value": "subdir"},
                                {"title": "递归扫描（网盘模式最多 3 层，请求较多）", "value": "recursive"},
                            ],
                        },
                    }, md=6),
                    text("file_interval", "文件写入间隔（秒）", "3",
                         tip="每次目录读取与文件写入前的等待，115 建议不低于 3 秒"),
                ),
                row(
                    text("movie_limit", "单次处理影片上限", "30", tip="每次任务最多处理多少部，剩余下次继续"),
                    col({
                        "component": "VRow",
                        "content": [
                            col({"component": "VTextField",
                                 "props": {"model": "cron_hour", "label": "定时-时(0-23)"}}, cols=6),
                            col({"component": "VTextField",
                                 "props": {"model": "cron_minute", "label": "定时-分(0-59)"}}, cols=6),
                        ],
                    }, md=6),
                ),
                row(
                    switch("enabled", "启用插件"),
                    switch("force_overwrite", "强制覆盖已有 NFO / 图片",
                           tip="关闭时跳过已存在 movie.nfo 的目录（增量模式）"),
                ),
                row(
                    col({
                        "component": "VBtn",
                        "props": {"color": "primary", "variant": "flat", "size": "small",
                                  "prepend-icon": "mdi-play", "onClick": JS_RUN},
                        "text": "立即执行刮削",
                    }, cols=12, md=3),
                    col({
                        "component": "VTextField",
                        "props": {"model": "run_msg", "label": "执行结果",
                                  "readonly": True, "variant": "plain"},
                    }, cols=12, md=9),
                ),
                row(
                    col({
                        "component": "VBtn",
                        "props": {"color": "secondary", "variant": "tonal", "size": "small",
                                  "prepend-icon": "mdi-flask", "onClick": JS_SELFTEST},
                        "text": "测试数据源",
                    }, cols=12, md=3),
                    col({
                        "component": "VTextField",
                        "props": {"model": "test_number", "label": "测试用番号",
                                  "placeholder": "SSIS-110"},
                    }, cols=12, md=3),
                    col({
                        "component": "VTextField",
                        "props": {"model": "run_msg", "label": "测试结果",
                                  "readonly": True, "variant": "plain"},
                    }, cols=12, md=6),
                ),
                row(
                    switch("download_previews", "下载剧照",
                           tip="直接下载源站原图，会增加文件写入量"),
                    col({"component": "VTextField",
                         "props": {"model": "preview_limit", "label": "剧照张数上限"}}, md=6),
                ),
                row(
                    switch("rename_files", "重命名视频文件为番号",
                           tip="115 上重命名属敏感操作，默认关闭"),
                    switch("numeric_ids", "识别无字母番号",
                           tip="加勒比 010115-001 / 一本道 061622_001 等，存在误识别风险"),
                ),
            ],
        }]

        data = {
            "enabled": False,
            "metatube_url": "",
            "metatube_token": "",
            "providers": "FANZA,JavBus,MGS,FC2PPVDB,fc2hub",
            "storage": "local",
            "browse_path": "/",
            "pick_dir": None,
            "dir_items": [],
            "dir_error": "",
            "run_msg": "",
            "test_number": "SSIS-110",
            "scan_dirs": "",
            "write_local": True,
            "write_remote": False,
            "remote_lib_root": "",
            "local_strm_root": "",
            "organize": False,
            "trigger_strm": True,
            "scan_mode": "subdir",
            "file_interval": 3,
            "movie_limit": 30,
            "cron_hour": 4,
            "cron_minute": 30,
            "force_overwrite": False,
            "download_previews": False,
            "preview_limit": 10,
            "rename_files": False,
            "numeric_ids": False,
        }
        return form, data

    def stop(self):
        self.stop_service()
