"""Persistent media IDs, output ownership and read-only 115 playback."""
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cloud115 import CloudError, DirectLink, Entry

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".rmvb",
              ".rm", ".mpg", ".mpeg", ".flv", ".iso", ".m4v"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked_path(root, path):
    root, path = Path(root), Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise CloudError('本地输出越出配置根目录') from None
    if len(relative.parts) != 3:
        raise CloudError('本地输出层级无效')
    current = root
    for part in relative.parts[:-1]:
        if current.is_symlink() or current.resolve() != current:
            raise CloudError('本地输出目录发生替换')
        current = current/part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise CloudError('本地输出目录不是普通目录')
    if current.resolve() != current or root.is_symlink() or root.resolve() != root:
        raise CloudError('本地输出目录发生替换')


class UnknownMedia(KeyError):
    """The capability is absent from this account registry."""


class Registry:
    def __init__(self, directory, output_root=None, readonly=False, create=True):
        self.directory = Path(directory)
        self.output_root = output_root
        self.readonly = readonly
        self.create = create and not readonly
        self.identity = None
        if self.create:
            self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = None
        self.reopen()

    def reopen(self):
        with self._lock:
            if self.db is not None:
                return
            path = self.directory / "direct.sqlite3"
            try:
                before = path.stat()
                identity = (before.st_dev, before.st_ino)
            except FileNotFoundError:
                identity = None
            if not self.create and identity is None:
                raise CloudError("播放登记数据库缺失，请检查插件数据目录或恢复备份")
            try:
                if self.create:
                    self.db = sqlite3.connect(str(path), check_same_thread=False)
                else:
                    mode = "ro" if self.readonly else "rw"
                    self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True, check_same_thread=False)
                self.db.row_factory = sqlite3.Row
                current = path.stat()
                self.identity = (current.st_dev, current.st_ino)
                if identity is not None and self.identity != identity:
                    raise CloudError("播放登记数据库在打开时被替换，请重新检查")
                if self.readonly:
                    self.db.execute("PRAGMA query_only=ON")
                    self.db.execute("SELECT token,account,fid,entry FROM media LIMIT 0")
                    return
            except (sqlite3.Error, OSError, CloudError):
                self.close()
                raise CloudError("播放登记数据库不可用或已被替换，请检查插件数据目录或备份") from None
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS media (
                    token TEXT PRIMARY KEY, account TEXT, fid TEXT, entry TEXT,
                    UNIQUE(account, fid));
                CREATE TABLE IF NOT EXISTS outputs (
                    path TEXT PRIMARY KEY, account TEXT, job TEXT,
                    digest TEXT, pending TEXT);
            """)
            self.db.commit()

    def check_identity(self):
        # A live SQLite connection can still read a deleted/replaced inode.
        # Never serve an old capability after the configured file disappeared.
        try:
            current = (self.directory / "direct.sqlite3").stat()
        except OSError:
            raise CloudError("播放登记数据库缺失，请检查插件数据目录或恢复备份") from None
        if (current.st_dev, current.st_ino) != self.identity:
            raise CloudError("播放登记数据库已被替换，请重新加载插件后重试")

    def close(self):
        with self._lock:
            if self.db is not None:
                self.db.close()
                self.db = None

    def _media_token(self, account, entry):
        # Acquire the SQLite write lock before reading. Independent Registry
        # connections/processes must never replace another returned capability.
        token = secrets.token_hex(32)
        self.db.execute("""INSERT INTO media(token,account,fid,entry) VALUES(?,?,?,?)
                        ON CONFLICT(account,fid) DO UPDATE SET entry=excluded.entry""",
                        (token, account, entry.id, json.dumps(asdict(entry))))
        return self.db.execute("SELECT token FROM media WHERE account=? AND fid=?",
                               (account, entry.id)).fetchone()["token"]

    def media_token(self, account, entry):
        with self._lock, self.db:
            self.check_identity()
            self.db.execute("BEGIN IMMEDIATE")
            return self._media_token(account, entry)

    def media(self, account, token):
        with self._lock:
            self.check_identity()
            row = self.db.execute("SELECT entry FROM media WHERE token=? AND account=?", (token, account)).fetchone()
            if row is None and self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_aliases'").fetchone():
                row = self.db.execute("""SELECT m.entry FROM media_aliases a JOIN media m
                        ON m.account=a.account AND m.fid=a.fid WHERE a.token=? AND a.account=?""",
                        (token, account)).fetchone()
            if row is None:
                raise UnknownMedia("未登记的播放标识")
            return Entry(**json.loads(row["entry"]))

    def _output(self, path):
        return self.db.execute("SELECT * FROM outputs WHERE path=?", (str(path),)).fetchone()

    def check_output(self, path, account, job):
        if self.output_root is not None:
            checked_path(self.output_root, path)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise CloudError('本地输出路径不是普通文件：' + str(path))
        with self._lock:
            row = self._output(path)
            if row and (row['account'], row['job']) != (account, job):
                raise CloudError(f"本地输出归属冲突 路径={str(path)!r} 已有账户={row['account']!r} 已有任务={row['job']!r} 当前账户={account!r} 当前任务={job!r}")
            if path.exists() and (not row or digest(path.read_bytes()) not in (row['digest'], row['pending'])):
                raise CloudError('保留未归属插件或已被修改的文件：' + str(path))

    def owned_bytes(self, path, account, job):
        self.check_output(path, account, job)
        return path.read_bytes() if path.exists() else None

    def reserve(self, path, account, job, data):
        with self._lock, self.db:
            self.check_output(path, account, job)
            row = self._output(path)
            confirmed = row["digest"] if row else None
            if row and row["pending"] and path.is_file() and digest(path.read_bytes()) == row["pending"]:
                # Publication may have succeeded before its acknowledgement was
                # saved. Preserve that verified content before replacing pending
                # with a new-format digest, so recovery never loses ownership.
                confirmed = row["pending"]
            self.db.execute("INSERT OR REPLACE INTO outputs VALUES(?,?,?,?,?)",
                            (str(path), account, job, confirmed, digest(data)))

    def published(self, path, data):
        with self._lock, self.db:
            self.db.execute("UPDATE outputs SET digest=?,pending=NULL WHERE path=?", (digest(data), str(path)))


class PlaybackService:
    """Playback opens existing state only; no library or empty database creation."""
    def __init__(self, client, data_dir):
        self.client = client
        self.registry = Registry(data_dir, readonly=True)
        self._cache = {}
        self._play_lock = threading.RLock()

    def close(self):
        self.registry.close()

    def playback_url(self, media_id, user_agent=""):
        entry = self.registry.media(self.client.account, media_id)
        key = (self.client.account, entry.id, user_agent)
        with self._play_lock:
            cached = self._cache.get(key)
            if cached and cached[1] > time.time():
                return cached[0]
            link = self.client.download_link(entry, user_agent)
            query = parse_qs(urlsplit(link.url).query)
            if any(k.lower() == "cookie" and v for k, v in link.headers.items()) or query.get("f") == ["3"]:
                raise CloudError("此 115 链接需要额外 Cookie，不能通过 302 直接播放")
            if urlsplit(link.url).scheme not in ("http", "https") or not urlsplit(link.url).hostname:
                raise CloudError("115 返回了无效的播放链接")
            if link.expires <= time.time() + 5:
                raise CloudError("115 播放链接已过期，请稍后重试")
            self._cache[key] = (link.url, min(link.expires - 5, time.time() + 60))
            # A library can be large; bound process-local URL cache growth.
            if len(self._cache) > 512:
                self._cache = {k: v for k, v in self._cache.items() if v[1] > time.time()}
                if len(self._cache) > 512:
                    self._cache.pop(next(iter(self._cache)))
            return link.url


def _part(name):
    match = re.search(r"(?:[-_.\s])(?:CD|DISC|PART)[-_.\s]?(\d+)(?=[-_.\s]|$)", Path(name).stem, re.I)
    return "-CD" + str(int(match[1])) if match else ""
