"""小雅本地索引库：入库、清洗与分享直链反解。"""

from __future__ import annotations

import io
import re
import sqlite3
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from app.log import logger

from .client import XiaoyaClient
from ..types import append_share_password, normalize_resource_type

_SCHEMA = """
CREATE TABLE IF NOT EXISTS xiaoya_shares (
    prefix TEXT PRIMARY KEY,
    drive_type TEXT NOT NULL,
    share_code TEXT NOT NULL,
    folder_id TEXT DEFAULT '',
    password TEXT DEFAULT ''
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS xiaoya_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    root_folder TEXT NOT NULL,
    title TEXT NOT NULL,
    clean_title TEXT NOT NULL,
    search_name TEXT DEFAULT '',
    douban_id TEXT DEFAULT '',
    category TEXT DEFAULT '',
    updated_at INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_xiaoya_res_clean_title ON xiaoya_resources(clean_title);
CREATE INDEX IF NOT EXISTS idx_xiaoya_res_search_name ON xiaoya_resources(search_name);
CREATE INDEX IF NOT EXISTS idx_xiaoya_res_folder_title ON xiaoya_resources(root_folder, clean_title);
CREATE INDEX IF NOT EXISTS idx_xiaoya_res_douban_id ON xiaoya_resources(douban_id);
CREATE TABLE IF NOT EXISTS xiaoya_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
"""


def clean_xiaoya_path(raw_path: str) -> Tuple[str, str]:
    """清洗小雅路径，返回 (标准相对路径, 一级分类)。

    网盘挂载点是一级「XX分享」容器目录，按该结构剥离，不硬编码网盘名。
    """
    p = str(raw_path or "").strip().lstrip("./").lstrip("/")
    if not p:
        return "", "其他"

    head, sep, tail = p.partition("/")
    if sep and tail and "分享" in head:
        p = tail

    parts = [seg for seg in (s.strip() for s in p.split("/")) if seg]
    if not parts:
        return "", "其他"
    return p, (re.sub(r"^\W+|\W+$", "", parts[0]) or parts[0])


_VIDEO_EXT_RE = re.compile(
    r"\.(mkv|mp4|iso|ts|avi|rmvb|flv|wmv|m2ts|mov|mpg|mpeg|rm|vob)$",
    re.IGNORECASE,
)
#: 技术规格词：出现即代表媒体名已结束，其后是清晰度/编码/字幕等发布标注
_SPEC_TOKEN_RE = re.compile(
    r"(?:"
    r"2160p|1080p|1080i|720p|480p|4k|8k|uhd|hd|sd|"
    r"web-?dl|web-?rip|blu-?ray|bluray|bdrip|bd|remux|hdtv|dvdrip|dvd|iso|"
    r"hdr10\+?|hdr|dv|dolby|atmos|truehd|dts(?:-hd)?|ddp|e?ac3|aac|flac|"
    r"h\.?26[45]|x26[45]|hevc|avc|xvid|divx|10bit|8bit|60fps|imax|"
    r"中英|中字|简中|繁中|简体|繁体|国语|粤语|双语|字幕|内嵌|外挂|无字幕|特效|"
    r"multi|chs|cht|"
    r"第\d{1,3}[集话期]|s\d{1,2}e\d{1,3}|ep?\d{1,3}|"
    r"proper|repack|internal|extended|uncut|remastered|complete|bluray"
    r")",
    re.IGNORECASE,
)


def clean_index_name(raw_name: str) -> str:
    """从小雅索引条目名推导媒体名：剥离发布组、清晰度与字幕等发布噪声。

    索引中大量条目直接取自文件名，发布组名（如 ``￡CMCT阿凡提``）会污染检索，
    使无关影视命中；这里只保留真正的媒体名，检索与展示都基于它。
    """
    name = str(raw_name or "").strip()
    if not name:
        return ""
    name = _VIDEO_EXT_RE.sub("", Path(name).name)
    # 发布组尾注：￡、◎ 之后以及 "@组织名" 均非媒体名组成部分
    name = re.split(r"[￡◎]|@(?=[A-Za-z0-9])", name, maxsplit=1)[0]
    # 方括号内为版本/字幕/地区标注
    name = re.sub(r"[\[【][^\]】]*[\]】]", " ", name)
    tokens = [tok for tok in re.split(r"[\s\.\-_]+", name) if tok]
    kept: List[str] = []
    for token in tokens:
        if _SPEC_TOKEN_RE.fullmatch(token):
            break
        kept.append(token)
    cleaned = " ".join(kept).strip()
    return cleaned or name.strip()


def _normalize_key(value: str) -> str:
    """检索键归一化：去分隔符并转小写，与 SQL 侧 LIKE 参数保持一致。"""
    return re.sub(r"[\s\.\-_]+", "", str(value or "")).lower()


class XiaoyaResourceService:
    """本地索引库与分享反解。"""

    def __init__(
            self,
            client: XiaoyaClient,
            data_dir: Path,
            auto_sync_days: int = 1,
    ):
        self._client = client
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "xiaoya_index.db"
        self.auto_sync_days = max(1, int(auto_sync_days or 1))

        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        # [(prefix, drive_type, share_code, folder_id, password)]
        self._share_cache: List[Tuple[str, str, str, str, str]] = []
        self._init_db()
        self._load_share_cache()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA mmap_size = 268435456;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """建表与索引（单次 executescript）。"""
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executescript(_SCHEMA)
            conn.commit()

    def _load_share_cache(self) -> None:
        """载入分享前缀，按长度降序排列供最长前缀匹配。"""
        with self._lock:
            try:
                with self._get_connection() as conn:
                    cur = conn.execute(
                        "SELECT prefix, drive_type, share_code, folder_id, password FROM xiaoya_shares"
                    )
                    rows = cur.fetchall()
                    loaded = [
                        (
                            str(r["prefix"]).strip().strip("/"),
                            normalize_resource_type(str(r["drive_type"]).strip()),
                            str(r["share_code"]).strip(),
                            str(r["folder_id"] or "").strip(),
                            str(r["password"] or "").strip(),
                        )
                        for r in rows
                        if r["prefix"] and r["share_code"]
                    ]
                    loaded.sort(key=lambda item: len(item[0]), reverse=True)
                    self._share_cache = loaded
            except Exception as error:
                logger.warning(f"小雅加载网盘分享映射缓存异常：{error}")

    def resolve_share(self, path: str) -> Optional[Dict[str, str]]:
        """按路径最长前缀反解网盘类型与真实分享链接。"""
        clean_path, _ = clean_xiaoya_path(path)
        if not clean_path:
            return None

        with self._lock:
            shares = self._share_cache

        for prefix, drive_type, share_code, folder_id, password in shares:
            if clean_path == prefix or clean_path.startswith(prefix + "/"):
                sub_path = clean_path[len(prefix):].lstrip("/")
                share_url = self._build_share_url(drive_type, share_code, password)
                return {
                    "drive_type": drive_type,
                    "share_code": share_code,
                    "folder_id": folder_id,
                    "password": password,
                    "share_url": share_url,
                    "mount_prefix": prefix,
                    "sub_path": sub_path,
                }
        return None

    @staticmethod
    def _build_share_url(drive_type: str, share_code: str, password: str = "") -> str:
        """拼装分享直链并附加提取码。"""
        norm_type = normalize_resource_type(drive_type)
        bases = {
            "115": f"https://115.com/s/{share_code}",
            "quark": f"https://pan.quark.cn/s/{share_code}",
            "alipan": f"https://www.alipan.com/s/{share_code}",
            "123": f"https://www.123pan.com/s/{share_code}",
            "pikpak": f"https://mypikpak.com/s/{share_code}",
        }
        raw_url = bases.get(norm_type, f"https://pan.quark.cn/s/{share_code}")
        return append_share_password(norm_type, raw_url, password)

    def get_meta(self, key: str, default: str = "") -> str:
        """读取元数据。"""
        try:
            with self._get_connection() as conn:
                cur = conn.execute("SELECT value FROM xiaoya_meta WHERE key = ?", (key,))
                row = cur.fetchone()
                return str(row["value"]) if row else default
        except Exception:
            return default

    def sync_index(self, force: bool = False) -> Dict[str, Any]:
        """拉取远程 txt 合并入库，支持 304 与增量更新。"""
        if not force and not self.needs_sync():
            status = self.get_status()
            status["message"] = "小雅索引处于有效期内，无需同步"
            return status

        if not self._sync_lock.acquire(blocking=False):
            return {"status": "running", "message": "小雅索引同步任务正在执行中..."}

        try:
            logger.debug("开始小雅索引库同步（304 条件缓存 + 增量入库）...")
            start_time = time.time()
            now_ts = int(time.time())

            # 1. 三类网盘分享映射（`prefix share_code folder_id [password]`），带 304 增量
            shares_by_drive: Dict[str, List[Tuple[str, str, str, str, str]]] = {}
            pending_meta: Dict[str, str] = {}
            for filename, drive_type, has_password in (
                    ("quarkshare_list.txt", "quark", False),
                    ("115share_list.txt", "115", True),
                    ("pikpakshare_list.txt", "pikpak", False),
            ):
                etag_key, lm_key = f"share_etag:{drive_type}", f"share_lm:{drive_type}"
                try:
                    raw, new_etag, new_lm = self._client.fetch_data_file(
                        filename,
                        etag=self.get_meta(etag_key, "") or None,
                        last_modified=self.get_meta(lm_key, "") or None,
                    )
                    if raw is None:            # 304：远端未变更，保留库内既有映射
                        continue
                    rows: List[Tuple[str, str, str, str, str]] = []
                    for line in raw.decode("utf-8", errors="ignore").splitlines():
                        parts = line.strip().split()
                        if len(parts) < 2:
                            continue
                        prefix, share_code = parts[0].strip().strip("/"), parts[1].strip()
                        if not (prefix and share_code):
                            continue
                        folder_id = parts[2].strip() if len(parts) >= 3 else ""
                        password = parts[3].strip() if has_password and len(parts) >= 4 else ""
                        rows.append((prefix, drive_type, share_code, folder_id, password))
                    shares_by_drive[drive_type] = rows
                    if new_etag:
                        pending_meta[etag_key] = new_etag
                    if new_lm:
                        pending_meta[lm_key] = new_lm
                except Exception as error:
                    logger.warning(f"下载小雅 {drive_type} 分享列表失败：{error}")
            share_count = sum(len(rows) for rows in shares_by_drive.values()) or self.get_status()["share_count"]

            # 2. index.zip 带 ETag/304 条件下载
            zip_etag = self.get_meta("index_zip_etag", "")
            zip_lm = self.get_meta("index_zip_lm", "")

            zip_bytes, new_etag, new_lm = self._client.fetch_data_file(
                "index.zip", etag=zip_etag or None, last_modified=zip_lm or None
            )

            is_zip_modified = zip_bytes is not None
            resources_data: List[Tuple[str, str, str, str, str, str, str, int]] = []

            if is_zip_modified:
                try:
                    seen_paths: Set[str] = set()
                    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                        for name in zf.namelist():
                            if not name.endswith(".txt") or name.startswith((".", "_")):
                                continue
                            if any(k in name for k in ("book", "music", "non.video")):
                                continue

                            # 文件名决定 category：movie / tv / comics / docu / media
                            if "movie" in name:
                                category = "movie"
                            elif "tv" in name:
                                category = "tv"
                            elif "comics" in name:
                                category = "comics"
                            elif "docu" in name:
                                category = "docu"
                            else:
                                category = "media"

                            content = zf.read(name).decode("utf-8", errors="ignore")
                            # 行格式：`path#title#douban_id#score#poster`
                            for line in content.splitlines():
                                line = line.strip()
                                if not line:
                                    continue
                                parts = line.split("#")
                                clean_path, root_folder = clean_xiaoya_path(parts[0].strip())
                                if not clean_path or clean_path in seen_paths:
                                    continue
                                seen_paths.add(clean_path)

                                title = parts[1].strip() if len(parts) > 1 and parts[1].strip() else Path(clean_path).name
                                douban_id = parts[2].strip() if len(parts) > 2 else ""
                                clean_title = _normalize_key(title)
                                # 检索名只保留媒体名，剥离发布组与清晰度等发布噪声
                                search_name = _normalize_key(
                                    clean_index_name(Path(clean_path).name) or title
                                )
                                resources_data.append((
                                    clean_path, root_folder, title, clean_title,
                                    search_name, douban_id, category, now_ts
                                ))
                except Exception as error:
                    logger.error(f"下载或解析小雅 index.zip 索引包异常：{error}")

            # 3. 事务批量写库
            with self._get_connection() as conn:
                conn.execute("PRAGMA synchronous = OFF")
                conn.execute("PRAGMA cache_size = -64000")

                for drive_type, rows in shares_by_drive.items():
                    conn.execute("DELETE FROM xiaoya_shares WHERE drive_type = ?", (drive_type,))
                    conn.executemany(
                        """INSERT OR REPLACE INTO xiaoya_shares 
                           (prefix, drive_type, share_code, folder_id, password) 
                           VALUES (?, ?, ?, ?, ?)""",
                        rows,
                    )

                if is_zip_modified and resources_data:
                    conn.executemany(
                        """INSERT INTO xiaoya_resources 
                           (path, root_folder, title, clean_title, search_name, douban_id, category, updated_at) 
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(path) DO UPDATE SET
                               root_folder = excluded.root_folder,
                               title = excluded.title,
                               clean_title = excluded.clean_title,
                               search_name = excluded.search_name,
                               douban_id = excluded.douban_id,
                               category = excluded.category,
                               updated_at = excluded.updated_at
                           WHERE excluded.updated_at > xiaoya_resources.updated_at""",
                        resources_data,
                    )

                    if new_etag:
                        conn.execute(
                            "INSERT OR REPLACE INTO xiaoya_meta (key, value) VALUES ('index_zip_etag', ?)",
                            (new_etag,),
                        )
                    if new_lm:
                        conn.execute(
                            "INSERT OR REPLACE INTO xiaoya_meta (key, value) VALUES ('index_zip_lm', ?)",
                            (new_lm,),
                        )

                for key, value in pending_meta.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO xiaoya_meta (key, value) VALUES (?, ?)",
                        (key, value),
                    )

                conn.execute(
                    "INSERT OR REPLACE INTO xiaoya_meta (key, value) VALUES ('last_sync', ?)",
                    (str(now_ts),),
                )
                conn.commit()
                conn.execute("PRAGMA synchronous = NORMAL")

            self._load_share_cache()
            duration = time.time() - start_time
            total_status = self.get_status()

            logger.info(
                f"小雅索引同步完成（资源 {total_status.get('resource_count')} 条，"
                f"挂载网盘 {share_count} 个，耗时 {duration:.2f}s）"
            )
            return {
                "status": "success",
                "total_resources": total_status.get("resource_count"),
                "share_count": share_count,
                "duration_seconds": round(duration, 2),
                "zip_modified": is_zip_modified,
            }
        finally:
            self._sync_lock.release()

    def rebuild_index(self) -> Dict[str, Any]:
        """删除本地数据库后全量重建。"""
        with self._sync_lock:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)
                except OSError as error:
                    logger.warning(f"清理小雅索引文件失败：{error}")
            self._share_cache = []
            self._init_db()
            self._load_share_cache()
        logger.info("小雅索引库已清空，开始全量重建...")
        return self.sync_index(force=True)

    def needs_sync(self) -> bool:
        """索引库是否需同步。"""
        try:
            with self._get_connection() as conn:
                cur = conn.execute("SELECT value FROM xiaoya_meta WHERE key = 'last_sync'")
                row = cur.fetchone()
                if not row:
                    return True
                last_time = int(row["value"] or 0)
                if time.time() - last_time > (self.auto_sync_days * 86400):
                    return True
                c_cur = conn.execute("SELECT COUNT(1) as cnt FROM xiaoya_resources")
                cnt_row = c_cur.fetchone()
                return not cnt_row or cnt_row["cnt"] == 0
        except Exception:
            return True

    def search(
            self,
            keyword: str,
            limit: int = 20,
            drive_types: Optional[Iterable[str]] = None,
            douban_id: Optional[str] = None,
            category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """检索索引库：支持豆瓣ID直达、分类与目录过滤。"""
        kw = keyword.strip()
        db_id = str(douban_id or "").strip()
        if not kw and not db_id:
            return []

        clean_kw = re.sub(r"[\s\.\-_]+", "", kw).lower() if kw else ""
        results: List[Dict[str, Any]] = []

        allowed_drives: Set[str] = {normalize_resource_type(d) for d in (drive_types or ()) if d.strip()}

        try:
            with self._get_connection() as conn:
                params: List[Any] = []
                # 豆瓣ID命中赋予极高权重（relevance=1），检索名精确匹配为2，前缀为3，其余包含为4
                sql = """
                    SELECT path, root_folder, title, douban_id, category, updated_at,
                           CASE 
                               WHEN douban_id != '' AND douban_id = ? THEN 1
                               WHEN search_name = ? OR clean_title = ? OR title = ? THEN 2
                               WHEN search_name LIKE ? OR clean_title LIKE ? OR title LIKE ? THEN 3
                               ELSE 4
                           END as relevance
                    FROM xiaoya_resources 
                    WHERE 1=1
                """
                prefix_kw = f"{clean_kw}%" if clean_kw else ""
                prefix_raw = f"{kw}%" if kw else ""
                substr_kw = f"%{clean_kw}%" if clean_kw else ""
                substr_raw = f"%{kw}%" if kw else ""

                params.extend([
                    db_id,
                    clean_kw, clean_kw, kw,
                    prefix_kw, prefix_kw, prefix_raw,
                ])

                # 匹配条件：豆瓣ID精确命中 或 检索名/标题模糊匹配。
                # 刻意不匹配原始 path：路径中的发布组名（如 `￡CMCT阿凡提`）会让无关影视误命中
                if db_id and kw:
                    sql += " AND (douban_id = ? OR search_name LIKE ? OR clean_title LIKE ? OR title LIKE ?)"
                    params.extend([db_id, substr_kw, substr_kw, substr_raw])
                elif db_id:
                    sql += " AND douban_id = ?"
                    params.append(db_id)
                else:
                    sql += " AND (search_name LIKE ? OR clean_title LIKE ? OR title LIKE ?)"
                    params.extend([substr_kw, substr_kw, substr_raw])

                if category:
                    sql += " AND (category = ? OR category = 'daily' OR category = 'media')"
                    params.append(category.lower())

                # 排序：相关度优先 -> 命中名越短越精炼（长名多为上游标注噪声）-> 最新更新优先
                sql += """
                    ORDER BY relevance ASC,
                             length(search_name) ASC,
                             updated_at DESC
                    LIMIT ?
                """
                params.append(max(limit * 10, 200))

                cur = conn.execute(sql, params)
                rows = cur.fetchall()

            seen_shares: Set[Tuple[str, str]] = set()
            for r in rows:
                path = r["path"]
                title = r["title"]

                resolved = self.resolve_share(path)
                if not resolved:
                    continue

                drive = resolved["drive_type"]
                if allowed_drives and drive not in allowed_drives:
                    continue

                # 按网盘、分享及具体影视相对路径去重，避免超大合集（如几千部合集包）
                # 因相同 share_code 被粗暴折叠为单一条目，确保索引库中命中的各部具体电影均能独立展示
                target_sub = resolved.get("sub_path") or path
                target_name = Path(target_sub).name
                share_key = (drive, resolved["share_code"], target_sub)
                if share_key in seen_shares:
                    continue
                seen_shares.add(share_key)

                results.append({
                    "path": path,
                    "root_folder": r["root_folder"],
                    "title": title,
                    "douban_id": r["douban_id"],
                    "category": r["category"],
                    "updated_at": r["updated_at"],
                    "drive_type": drive,
                    "share_url": resolved["share_url"],
                    "share_code": resolved["share_code"],
                    "password": resolved["password"],
                    "folder_id": resolved["folder_id"],
                    "mount_prefix": resolved["mount_prefix"],
                    "sub_path": resolved["sub_path"],
                    "target_name": target_name,
                })
                if len(results) >= limit:
                    break

        except Exception as error:
            logger.error(f"小雅本地索引查询异常（keyword={kw}, douban_id={db_id}）：{error}")

        return results

    def get_status(self) -> Dict[str, Any]:
        """索引库状态统计。"""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT (SELECT COUNT(1) FROM xiaoya_resources) AS res_cnt,
                       (SELECT COUNT(1) FROM xiaoya_shares) AS share_cnt,
                       (SELECT value FROM xiaoya_meta WHERE key = 'last_sync') AS last_sync
            """).fetchone()
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "resource_count": row["res_cnt"],
            "share_count": row["share_cnt"],
            "last_sync_time": int(row["last_sync"] or 0),
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "is_ready": row["res_cnt"] > 0 and row["share_cnt"] > 0,
        }

    def clear_cache(self) -> int:
        """重载内存分享缓存。"""
        self._load_share_cache()
        return 0
