"""Independent 115 workflow: plan, journal, stage, rename, publish and play."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cloud115 import (AuthenticationError, Cloud115, CloudError, DirectLink,
                       Entry, RiskError, Stopped)

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".rmvb",
              ".rm", ".mpg", ".mpeg", ".flv", ".iso", ".m4v"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


class UnknownMedia(KeyError):
    """The capability is absent from this account registry."""


class Registry:
    def __init__(self, directory, output_root=None, readonly=False, create=True, expected_identity=None):
        self.directory = Path(directory)
        self.output_root = output_root
        self.readonly = readonly
        self.create = create and not readonly
        self.expected_identity = expected_identity
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
            if self.expected_identity is not None and identity != self.expected_identity:
                raise CloudError("播放登记数据库在检查后被替换，请重新检查")
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
                CREATE TABLE IF NOT EXISTS jobs (
                    account TEXT, job TEXT, plan TEXT, complete INTEGER DEFAULT 0,
                    PRIMARY KEY(account, job));
                CREATE TABLE IF NOT EXISTS media (
                    token TEXT PRIMARY KEY, account TEXT, fid TEXT, entry TEXT,
                    UNIQUE(account, fid));
                CREATE TABLE IF NOT EXISTS media_aliases (
                    token TEXT PRIMARY KEY, account TEXT NOT NULL, fid TEXT NOT NULL);
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

    def job(self, account, job):
        with self._lock:
            row = self.db.execute("SELECT * FROM jobs WHERE account=? AND job=?", (account, job)).fetchone()
            return (json.loads(row["plan"]), bool(row["complete"])) if row else (None, False)

    def save_job(self, account, job, plan, complete=False):
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?)",
                            (account, job, json.dumps(plan, ensure_ascii=False), int(complete)))

    def pending_archives(self, account):
        with self._lock:
            rows = self.db.execute(
                "SELECT job,plan FROM jobs WHERE account=? AND plan LIKE ?", (account, '%"archive_to"%'))
            return {row["job"]: target for row in rows
                    if (target := json.loads(row["plan"]).get("archive_to"))}

    def finish_archive(self, account, job, target_job, plan, entries):
        relocated = dict(plan)
        relocated.pop("archive_to")
        relocated["files"] = [
            dict(item, entry=asdict(entries[item["entry"]["id"]])) for item in plan["files"]
        ]
        with self._lock, self.db:
            self.check_identity()
            self.db.execute("BEGIN IMMEDIATE")
            if self.job(account, job) != (plan, True) or self.job(account, target_job)[0] is not None:
                raise CloudError("归档记录已变化，请检查目标番号目录")
            self.db.execute("UPDATE jobs SET job=?,plan=? WHERE account=? AND job=?",
                            (target_job, json.dumps(relocated, ensure_ascii=False), account, job))
            self.db.execute("UPDATE outputs SET job=? WHERE account=? AND job=?", (target_job, account, job))
            for item in relocated["files"]:
                if item["kind"] == "video":
                    entry = item["entry"]
                    self.db.execute("UPDATE media SET entry=? WHERE account=? AND fid=?",
                                    (json.dumps(entry), account, entry["id"]))

    def _media_token(self, account, entry):
        # Acquire the SQLite write lock before reading. Independent Registry
        # connections/processes must never replace another returned capability.
        token = secrets.token_hex(32)
        if self.db.execute("SELECT 1 FROM media_aliases WHERE token=?", (token,)).fetchone():
            raise CloudError("播放标识冲突，请重试")
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

    def restore_media(self, account, entry, token, expected=None):
        """Add an explicitly verified legacy capability without rebinding IDs.

        expected is an optional snapshot of affected rows from a repair preview;
        it is checked after taking the SQLite write lock.
        """
        if not re.fullmatch(r"[a-f0-9]{64}", token) or entry.is_dir or not entry.pickcode:
            raise CloudError("恢复播放登记的标识或视频信息无效")
        with self._lock, self.db:
            self.check_identity()
            self.db.execute("BEGIN IMMEDIATE")
            self.check_identity()
            if expected is not None and self.snapshot(account, entry.id, token) != expected:
                raise CloudError("播放登记已在检查后变化，请重新检查")
            for table in ("media", "media_aliases"):
                row = self.db.execute("SELECT account,fid FROM " + table + " WHERE token=?", (token,)).fetchone()
                if row and (row["account"], row["fid"]) != (account, entry.id):
                    raise CloudError("旧播放标识已属于其他账户或文件，已跳过")
                if row:
                    return False
            self._media_token(account, entry)
            self.db.execute("INSERT INTO media_aliases(token,account,fid) VALUES(?,?,?)", (token, account, entry.id))
            return True

    def snapshot(self, account, fid, token):
        """Only the affected records, used to reject stale repair previews."""
        with self._lock:
            self.check_identity()
            rows = [tuple(r) for r in self.db.execute(
                "SELECT token,account,fid,entry FROM media WHERE token=? OR (account=? AND fid=?) ORDER BY token",
                (token, account, fid))]
            aliases = []
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_aliases'").fetchone():
                aliases = [tuple(r) for r in self.db.execute(
                    "SELECT token,account,fid FROM media_aliases WHERE token=?", (token,))]
            return (rows, aliases)

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
        with self._lock:
            if self.output_root is not None:
                if (self.output_root.is_symlink() or self.output_root.resolve() != self.output_root
                        or path.parent.parent != self.output_root or path.parent.is_symlink()
                        or path.parent.resolve() != path.parent):
                    raise CloudError("本地输出目录发生替换或越出配置根目录")
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise CloudError("本地输出路径不是普通文件：" + path.name)
            row = self._output(path)
            if row and (row["account"], row["job"]) != (account, job):
                raise CloudError("本地输出已属于其他影片或账户：" + path.name)
            if path.exists():
                if not row or digest(path.read_bytes()) not in (row["digest"], row["pending"]):
                    raise CloudError("保留未归属插件或已被手动修改的文件：" + path.name)

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


class DirectService(PlaybackService):
    def __init__(self, client, data_dir, output_root, playback_base_url, metadata,
                 artwork, build_nfo, extract_number, stop_event=None, force=False, plugin_id="MetaTubeScraper",
                 debug=None, info=None, archive_loose=False):
        self.client = client
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", plugin_id):
            raise CloudError("插件实例标识无效")
        self.plugin_id = plugin_id
        if not Path(output_root).is_absolute():
            raise CloudError("本地媒体库根目录必须使用绝对路径，例如 /media/link/japan")
        self.root = Path(output_root).resolve()
        self.base = playback_base_url.rstrip("/")
        parts = urlsplit(self.base)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.query or parts.fragment:
            raise CloudError("请填写播放器可访问的 MoviePilot HTTP(S) 地址")
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(data_dir, self.root)
        self.metadata, self.artwork = metadata, artwork
        self.build_nfo, self.extract_number = build_nfo, extract_number
        self._debug = debug or (lambda message: None)
        self._info = info or (lambda message: None)
        self.stop_event = stop_event or threading.Event()
        self.force = force
        self.archive_loose = archive_loose
        self._cache = {}
        self._play_lock = threading.RLock()

    def close(self):
        self.registry.close()

    def _stopped(self):
        if self.stop_event.is_set():
            raise Stopped("任务已停止")

    def _plan(self, number, videos, entries, archive=False):
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]*", number):
            raise CloudError("番号不适合作为输出目录")
        all_videos = [e for e in entries if not e.is_dir and Path(e.name).suffix.lower() in VIDEO_EXTS]
        names, plans = set(), []
        video_ids = {e.id for e in videos}
        for entry in videos:
            stem = number + _part(entry.name)
            if stem.casefold() in names:
                raise CloudError("同番号存在不同版本或重复分段，请先分目录处理")
            names.add(stem.casefold())
            plans.append({"entry": asdict(entry), "target": stem + Path(entry.name).suffix.lower(), "kind": "video"})
        stems = {p["entry"]["id"]: Path(p["target"]).stem for p in plans}
        for entry in entries:
            if entry.is_dir or Path(entry.name).suffix.lower() not in SUBTITLE_EXTS:
                continue
            source_stem = Path(entry.name).stem
            matches = []
            for video in all_videos:
                original = Path(video.name).stem
                canonical = (self.extract_number(video.name) or number) + _part(video.name)
                for alias in dict.fromkeys((original, canonical)):
                    tail = source_stem[len(alias):]
                    if source_stem.casefold().startswith(alias.casefold()) and (not tail or tail[0] in ".-_ "):
                        matches.append((len(alias), video, tail))
            if matches:
                length = max(m[0] for m in matches)
                matches = [m for m in matches if m[0] == length]
            elif archive:
                # Loose subtitles can belong to videos elsewhere in this root.
                continue
            elif len(all_videos) == 1:
                tail = source_stem
                other_number = self.extract_number(tail)
                if other_number and other_number != number:
                    continue
                matches = [(0, all_videos[0], tail)]
            if len(matches) != 1:
                raise CloudError("同目录字幕无法唯一关联视频，请先整理字幕名称")
            _, owner, tail = matches[0]
            if owner.id not in video_ids:
                continue
            tail = tail.strip(" .-_")
            suffix = ("." + tail) if tail else ""
            target = stems[owner.id] + suffix + Path(entry.name).suffix.lower()
            if any(char in target for char in "/\\\r\n\0"):
                raise CloudError("字幕文件名包含不支持的路径字符")
            plans.append({"entry": asdict(entry), "target": target, "kind": "subtitle"})
        targets = set()
        for plan in plans:
            target = plan["target"].casefold()
            if target in targets:
                raise CloudError("规范命名后产生同名文件，已跳过")
            targets.add(target)
            if any(e.name.casefold() == target and e.id != plan["entry"]["id"] for e in entries):
                raise CloudError("云盘已存在目标名称，保留原文件")
        for plan in plans:
            if plan["target"].endswith(".idx") and plan["target"][:-4].casefold() + ".sub" not in targets:
                raise CloudError("IDX 字幕缺少配对的 SUB 文件")
        return {"number": number, "files": plans, "metadata": None}

    def _folder(self, number):
        folder = self.root / number
        if folder.is_symlink() or folder.resolve().parent != self.root:
            raise CloudError("本地影片目录越出输出根目录")
        folder.mkdir(exist_ok=True)
        return folder

    def _output_names(self, plan):
        names = [Path(p["target"]).stem + ".strm" if p["kind"] == "video" else p["target"] for p in plan["files"]]
        return names + ["movie.nfo", "poster.jpg", "fanart.jpg"]

    def _render_nfo(self, info, number):
        """Use one deterministic rendering for publication and format upgrades."""
        nfo = ET.fromstring(self.build_nfo(info, number))
        art = nfo.find("art")
        if art is None:
            art = ET.SubElement(nfo, "art")
        for kind, filename in (("poster", "poster.jpg"), ("fanart", "fanart.jpg")):
            node = art.find(kind)
            if node is None:
                node = ET.SubElement(art, kind)
            node.text = filename
        ET.indent(nfo, space="  ")
        return ET.tostring(nfo, encoding="utf-8", xml_declaration=True)

    def _check_folder(self, folder, identity):
        if (self.root.is_symlink() or self.root.resolve() != self.root or folder.is_symlink()
                or folder.resolve().parent != self.root):
            raise CloudError("本地输出目录发生替换，已停止处理")
        current = folder.stat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise CloudError("本地输出目录身份发生变化，已停止处理")

    def _process(self, number, videos, entries, archive=False):
        self._stopped()
        account = self.client.account
        job = videos[0].parent_id + ":" + number
        plan, complete = self.registry.job(account, job)
        was_complete = complete
        if plan is None:
            plan = self._plan(number, videos, entries, archive)
            info = self.metadata(number)
            if not info or not info.get("title"):
                return "notfound"
            plan["metadata"] = info
            self.registry.save_job(account, job, plan)
        elif not {p["entry"]["id"] for p in plan["files"] if p["kind"] == "video"}.issubset({e.id for e in videos}):
            raise CloudError("已登记的视频被移除或替换，请先处理版本冲突")
        # Recheck IDs, source names, parent directories and target collisions, including retries.
        actual = {e.id: e for e in self.client.listdir(videos[0].parent_id)}
        for item in plan["files"]:
            original = Entry(**item["entry"])
            current = actual.get(original.id)
            if not current or current.parent_id != original.parent_id or current.name not in (original.name, item["target"]):
                raise CloudError("计划中的文件已被移动或改名，请检查原目录")
            if any(e.id != original.id and e.name.casefold() == item["target"].casefold() for e in actual.values()):
                raise CloudError("云盘目标名称发生冲突，已停止该影片")
        # Reconcile newly arrived subtitles/parts against the recorded original
        # stems, so a partially renamed video cannot orphan its old-name subtitles.
        originals = {p["entry"]["id"]: p["entry"]["name"] for p in plan["files"]}
        naming_entries = [replace(e, name=originals.get(e.id, e.name)) for e in actual.values()]
        video_ids = {e.id for e in videos}
        reconciled = self._plan(number, [e for e in naming_entries if e.id in video_ids], naming_entries, archive)
        if {p["entry"]["id"] for p in reconciled["files"]} != set(originals):
            reconciled["metadata"] = plan["metadata"]
            plan = reconciled
            complete = False
            self.registry.save_job(account, job, plan)
        if self.force and was_complete:
            info = self.metadata(number)
            if not info or not info.get("title"):
                return "notfound"
            plan["metadata"] = info
        folder = self._folder(number)
        folder_identity = folder.stat()
        outputs = [folder / name for name in self._output_names(plan)]
        for path in outputs:
            self.registry.check_output(path, account, job)
        nfo_bytes = self._render_nfo(plan["metadata"], number)
        if (complete and not self.force and all(path.is_file() for path in outputs)
                and all(actual[p["entry"]["id"]].name == p["target"] for p in plan["files"])
                and (folder / "movie.nfo").read_bytes() == nfo_bytes):
            return "skip"
        self.registry.save_job(account, job, plan, complete=False)
        info = plan["metadata"]
        with tempfile.TemporaryDirectory(prefix=".metatube-", dir=str(self.root)) as temporary:
            stage = Path(temporary)
            data = {}
            for item in plan["files"]:
                self._stopped()
                entry = actual[item["entry"]["id"]]
                if item["kind"] == "video":
                    token = self.registry.media_token(account, entry)
                    name = Path(item["target"]).stem + ".strm"
                    data[name] = (self.base + "/api/v1/plugin/" + self.plugin_id + "/redirect_url?media_id=" + token + "\n").encode()
                else:
                    name = item["target"]
                    existing = self.registry.owned_bytes(folder / name, account, job)
                    data[name] = existing if existing is not None and not self.force else self.client.download_bytes(entry)
                    if not data[name]:
                        raise CloudError("字幕下载结果为空")
            for kind, filename in (("poster", "poster.jpg"), ("fanart", "fanart.jpg")):
                existing = self.registry.owned_bytes(folder / filename, account, job)
                data[filename] = existing if existing is not None and not self.force else self.artwork(info, kind)
                if not data[filename]:
                    raise CloudError("图片下载失败：" + filename)
            data["movie.nfo"] = nfo_bytes
            for name, content in data.items():
                self._check_folder(folder, folder_identity)
                (stage / name).write_bytes(content)
                self.registry.reserve(folder / name, account, job, content)
            self._check_folder(folder, folder_identity)
            latest = self.client.listdir(videos[0].parent_id)
            for item in plan["files"]:
                if any(e.id != item["entry"]["id"] and e.name.casefold() == item["target"].casefold() for e in latest):
                    raise CloudError("暂存期间云盘出现目标重名文件，已停止改名")
            for item in plan["files"]:
                self._stopped()
                self._check_folder(folder, folder_identity)
                entry = self.client.info(item["entry"]["id"])
                if entry.name != item["target"]:
                    if entry.name != item["entry"]["name"] or entry.parent_id != item["entry"]["parent_id"]:
                        raise CloudError("文件状态在执行前变化，已停止改名")
                    self.client.rename(entry.id, item["target"])
                    verified = self.client.info(entry.id)
                    if verified.name != item["target"] or verified.parent_id != entry.parent_id:
                        raise CloudError("云盘改名结果无法确认，下次执行将重新核验")
                item["renamed"] = True
                self.registry.save_job(account, job, plan)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(str(folder), flags)
            try:
                opened = os.fstat(directory_fd)
                if (opened.st_dev, opened.st_ino) != (folder_identity.st_dev, folder_identity.st_ino):
                    raise CloudError("本地输出目录身份发生变化")
                for name, content in data.items():
                    self._stopped()
                    self._check_folder(folder, folder_identity)
                    target = folder / name
                    self.registry.check_output(target, account, job)
                    if target.is_file() and target.read_bytes() == content:
                        self.registry.published(target, content)
                        continue
                    if target.exists():
                        os.replace(str(stage / name), name, dst_dir_fd=directory_fd)
                    else:
                        # Directory-relative exclusive publication never follows a
                        # replacement parent symlink or overwrites a late user file.
                        os.link(str(stage / name), name, dst_dir_fd=directory_fd, follow_symlinks=False)
                    self.registry.published(target, content)
            finally:
                os.close(directory_fd)
            self.registry.save_job(account, job, plan, complete=True)
        return "ok"

    @staticmethod
    def _archive_destination(entries, number):
        matches = [entry for entry in entries if entry.name.casefold() == number.casefold()]
        if not matches:
            return None
        if len(matches) != 1 or not matches[0].is_dir or matches[0].name != number:
            raise CloudError("目标番号目录名称冲突，已跳过归档")
        return matches[0].id

    def _archive(self, parent, number, destination):
        self._stopped()
        self.registry.check_identity()
        account, job = self.client.account, f"{parent}:{number}"
        plan, complete = self.registry.job(account, job)
        if not complete:
            raise CloudError("本地刮削尚未完成，已跳过归档")
        source = self.client.listdir(parent)
        existing = self._archive_destination(source, number)
        if existing != destination:
            raise CloudError("目标番号目录已变化，已跳过归档")
        if destination is None:
            self._stopped()
            destination = self.client.mkdir(number, parent)
            source = self.client.listdir(parent)
            if self._archive_destination(source, number) != destination:
                raise CloudError("无法确认新建的目标番号目录，已跳过归档")
        target_job = f"{destination}:{number}"
        if self.registry.job(account, target_job)[0] is not None:
            raise CloudError("目标番号目录已有刮削记录，已跳过归档")

        # Preserve the original job and output ownership until all moved IDs
        # have been verified. A partial move can resume even if no video is
        # left in the source directory.
        if plan.get("archive_to") != destination:
            plan["archive_to"] = destination
            self.registry.save_job(account, job, plan, complete=True)
        expected = {item["entry"]["id"]: item["target"] for item in plan["files"]}
        source_files = {entry.id: entry for entry in source}
        target_files = {entry.id: entry for entry in self.client.listdir(destination)}
        if any(entry.is_dir or expected.get(fid) != entry.name for fid, entry in target_files.items()):
            raise CloudError("目标番号目录包含其他文件，已跳过归档")
        remaining = [fid for fid in expected if fid not in target_files]
        for fid in remaining:
            entry = source_files.get(fid)
            if not entry or entry.is_dir or entry.name != expected[fid]:
                raise CloudError("待归档文件已移动或改名，请检查原目录")
        for name in self._output_names(plan):
            path = self.root / number / name
            self.registry.check_output(path, account, job)
            if not path.is_file():
                raise CloudError("本地产物不完整，已暂停归档")
        target = self.client.info(destination)
        if not target.is_dir or target.name != number or target.parent_id != parent:
            raise CloudError("目标番号目录身份已变化，已跳过归档")
        self._stopped()
        if remaining:
            self.registry.check_identity()
            self.client.move(remaining, destination)
            target_files = {entry.id: entry for entry in self.client.listdir(destination)}
        if (set(target_files) != set(expected)
                or any(entry.is_dir or entry.name != expected[fid] for fid, entry in target_files.items())):
            raise CloudError("归档未完成，下次运行将核对并继续")
        self.registry.finish_archive(account, job, target_job, plan, target_files)
        self._info(f"[{number}] 网盘归档完成，已移动至同名子目录")
        return destination

    def _check_movie_folder(self, cid, parent, original_name, number):
        siblings = self.client.listdir(parent)
        folder = next((entry for entry in siblings if entry.id == cid), None)
        if not folder or not folder.is_dir or folder.name not in (original_name, number):
            raise CloudError("影片目录位置或名称已变化，已跳过目录改名")
        if any(entry.id != cid and entry.name.casefold() == number.casefold() for entry in siblings):
            raise CloudError("同级已存在同名番号目录或文件，已跳过目录改名")
        return folder

    def _rename_movie_folder(self, cid, parent, original_name, number):
        plan, complete = self.registry.job(self.client.account, f"{cid}:{number}")
        if not complete:
            raise CloudError("本地刮削尚未完成，已跳过目录改名")
        expected = {item["entry"]["id"]: item["target"] for item in plan["files"]}
        current = {entry.id: entry for entry in self.client.listdir(cid)}
        if (any(entry.is_dir or (Path(entry.name).suffix.lower() in VIDEO_EXTS and fid not in expected)
                for fid, entry in current.items())
                or any(fid not in current or current[fid].name != name for fid, name in expected.items())):
            raise CloudError("影片目录内容已变化，已跳过目录改名")
        folder = self._check_movie_folder(cid, parent, original_name, number)
        if folder.name == number:
            return
        self._stopped()
        self.registry.check_identity()
        self.client.rename(cid, number)
        verified = self.client.info(cid)
        if not verified.is_dir or verified.name != number or verified.parent_id != parent:
            raise CloudError("网盘目录改名结果无法确认，下次执行将重新核验")
        self._info(f"[{number}] 网盘目录已改名：{original_name!r} → {number!r}")

    def run(self, roots, recursive=False, limit=30, max_depth=3):
        stats = dict(total=0, ok=0, skip=0, nomatch=0, notfound=0, fail=0, errors=[])
        try:
            self._run(roots, recursive, limit, max_depth, stats)
        except (AuthenticationError, RiskError) as exc:
            exc.stats = stats
            raise
        except Stopped:
            stats["stopped"] = True
        if self.stop_event.is_set():
            stats["stopped"] = True
        return stats

    def _run(self, roots, recursive, limit, max_depth, stats):
        if self.stop_event.is_set():
            return stats
        queue = []
        for root in roots:
            cid = self.client.resolve_folder(root)
            folder_name = self.client.info(cid).name if cid != "0" else ""
            queue.append((cid, 0, folder_name, None))
        visited = set()
        pending = self.registry.pending_archives(self.client.account)
        blocked_dirs = set(pending.values())
        directory_path = getattr(self.client, "directory_path", lambda cid: None)
        while queue and not self.stop_event.is_set():
            cid, depth, folder_name, parent_id = queue.pop(0)
            if cid in visited or cid in blocked_dirs:
                continue
            visited.add(cid)
            entries = self.client.listdir(cid)
            display = directory_path(cid) or folder_name or "/"
            if depth == 0:
                self._info(f"扫描目录已解析：{display!r}")
            groups = {}
            video_count = 0
            for entry in entries:
                if entry.is_dir:
                    if depth < (max_depth if recursive else 1):
                        queue.append((entry.id, depth + 1, entry.name, cid))
                elif Path(entry.name).suffix.lower() in VIDEO_EXTS:
                    video_count += 1
                    number = self.extract_number(entry.name)
                    source = "文件名"
                    if not number:
                        number = self.extract_number(folder_name)
                        source = "目录名" if number else "无"
                    self._debug(f"番号识别 目录={display!r} 原文件名={entry.name!r} 番号={number or '未识别'} 来源={source}")
                    if number:
                        groups.setdefault(number, []).append(entry)
                    else:
                        stats["nomatch"] += 1
                else:
                    self._debug(f"扫描文件 目录={display!r} 原文件名={entry.name!r} 类型=非视频")
            if depth == 0:
                for job in pending:
                    parent, _, number = job.partition(":")
                    if parent == cid:
                        groups.setdefault(number, [])
            for number, videos in groups.items():
                if self.stop_event.is_set() or stats["ok"] + stats["notfound"] + stats["fail"] >= limit:
                    return stats
                stats["total"] += 1
                try:
                    destination = pending.get(f"{cid}:{number}")
                    archive = self.archive_loose and depth == 0 and folder_name.casefold() != number.casefold()
                    if destination:
                        if archive:
                            self._archive(cid, number, destination)
                            status = "ok"
                        else:
                            self._debug(f"[{number}] 已暂停未完成的网盘归档")
                            status = "skip"
                    else:
                        rename_folder = (
                            self.archive_loose and parent_id is not None and folder_name != number
                            and len(groups) == 1 and len(videos) == video_count
                            and not any(entry.is_dir for entry in entries)
                        )
                        if rename_folder:
                            self._check_movie_folder(cid, parent_id, folder_name, number)
                        if archive:
                            destination = self._archive_destination(entries, number)
                            if destination:
                                blocked_dirs.add(destination)
                                if self.client.listdir(destination) or self.registry.job(
                                        self.client.account, f"{destination}:{number}")[0] is not None:
                                    raise CloudError("目标番号目录已含文件或刮削记录，已跳过归档")
                        status = self._process(number, videos, entries, archive)
                        if archive and status in ("ok", "skip"):
                            blocked_dirs.add(self._archive(cid, number, destination))
                            status = "ok"
                        elif rename_folder and status in ("ok", "skip"):
                            self._rename_movie_folder(cid, parent_id, folder_name, number)
                            status = "ok"
                except (AuthenticationError, RiskError):
                    raise
                except Stopped:
                    raise
                except Exception as exc:
                    status = "fail"
                    message = str(exc) if isinstance(exc, CloudError) else type(exc).__name__
                    stats["errors"].append(number + "：" + message)
                stats[status] += 1
                reason = " 原因=已有完整产物" if status == "skip" and not destination else ""
                self._debug(f"刮削结果 番号={number} 状态={status}{reason}")
        return stats
