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


class Registry:
    def __init__(self, directory, output_root=None):
        self.directory = Path(directory)
        self.output_root = output_root
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = None
        self.reopen()

    def reopen(self):
        with self._lock:
            if self.db is not None:
                return
            self.db = sqlite3.connect(str(self.directory / "direct.sqlite3"), check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    account TEXT, job TEXT, plan TEXT, complete INTEGER DEFAULT 0,
                    PRIMARY KEY(account, job));
                CREATE TABLE IF NOT EXISTS media (
                    token TEXT PRIMARY KEY, account TEXT, fid TEXT, entry TEXT,
                    UNIQUE(account, fid));
                CREATE TABLE IF NOT EXISTS outputs (
                    path TEXT PRIMARY KEY, account TEXT, job TEXT,
                    digest TEXT, pending TEXT);
            """)
            self.db.commit()

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

    def media_token(self, account, entry):
        with self._lock, self.db:
            row = self.db.execute("SELECT token FROM media WHERE account=? AND fid=?", (account, entry.id)).fetchone()
            token = row["token"] if row else secrets.token_hex(32)
            self.db.execute("INSERT OR REPLACE INTO media VALUES(?,?,?,?)",
                            (token, account, entry.id, json.dumps(asdict(entry))))
            return token

    def media(self, account, token):
        with self._lock:
            row = self.db.execute("SELECT entry FROM media WHERE token=? AND account=?", (token, account)).fetchone()
            if row is None:
                raise KeyError("未登记的播放标识")
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
            self.db.execute("INSERT OR REPLACE INTO outputs VALUES(?,?,?,?,?)",
                            (str(path), account, job, row["digest"] if row else None, digest(data)))

    def published(self, path, data):
        with self._lock, self.db:
            self.db.execute("UPDATE outputs SET digest=?,pending=NULL WHERE path=?", (digest(data), str(path)))


def _part(name):
    match = re.search(r"(?:[-_.\s])(?:CD|DISC|PART)[-_.\s]?(\d+)(?=[-_.\s]|$)", Path(name).stem, re.I)
    return "-CD" + str(int(match[1])) if match else ""


class DirectService:
    def __init__(self, client, data_dir, output_root, playback_base_url, metadata,
                 artwork, build_nfo, extract_number, stop_event=None, force=False):
        self.client = client
        self.root = Path(output_root).resolve()
        self.base = playback_base_url.rstrip("/")
        parts = urlsplit(self.base)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.query or parts.fragment:
            raise CloudError("请填写播放器可访问的 MoviePilot HTTP(S) 地址")
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(data_dir, self.root)
        self.metadata, self.artwork = metadata, artwork
        self.build_nfo, self.extract_number = build_nfo, extract_number
        self.stop_event = stop_event or threading.Event()
        self.force = force
        self._cache = {}
        self._play_lock = threading.RLock()

    def close(self):
        self.registry.close()

    def _stopped(self):
        if self.stop_event.is_set():
            raise Stopped("任务已停止")

    def _plan(self, number, videos, entries):
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

    def _check_folder(self, folder, identity):
        if (self.root.is_symlink() or self.root.resolve() != self.root or folder.is_symlink()
                or folder.resolve().parent != self.root):
            raise CloudError("本地输出目录发生替换，已停止处理")
        current = folder.stat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise CloudError("本地输出目录身份发生变化，已停止处理")

    def _process(self, number, videos, entries):
        self._stopped()
        account = self.client.account
        job = videos[0].parent_id + ":" + number
        plan, complete = self.registry.job(account, job)
        was_complete = complete
        if plan is None:
            plan = self._plan(number, videos, entries)
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
        reconciled = self._plan(number, [e for e in naming_entries if e.id in video_ids], naming_entries)
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
        if complete and not self.force and all(path.is_file() for path in outputs) and all(actual[p["entry"]["id"]].name == p["target"] for p in plan["files"]):
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
                    data[name] = (self.base + "/api/v1/plugin/MetaTubeScraper/redirect_url?media_id=" + token + "\n").encode()
                else:
                    name = item["target"]
                    existing = self.registry.owned_bytes(folder / name, account, job)
                    data[name] = existing if existing is not None and not self.force else self.client.download_bytes(entry)
                    if not data[name]:
                        raise CloudError("字幕下载结果为空")
            nfo = ET.fromstring(self.build_nfo(info, number))
            art = nfo.find("art")
            if art is None:
                art = ET.SubElement(nfo, "art")
            for kind, filename in (("poster", "poster.jpg"), ("fanart", "fanart.jpg")):
                node = art.find(kind)
                if node is None:
                    node = ET.SubElement(art, kind)
                node.text = filename
                existing = self.registry.owned_bytes(folder / filename, account, job)
                data[filename] = existing if existing is not None and not self.force else self.artwork(info, kind)
                if not data[filename]:
                    raise CloudError("图片下载失败：" + filename)
            data["movie.nfo"] = ET.tostring(nfo, encoding="utf-8", xml_declaration=True)
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

    def run(self, roots, recursive=False, limit=30, max_depth=3):
        stats = dict(total=0, ok=0, skip=0, nomatch=0, notfound=0, fail=0, errors=[])
        try:
            return self._run(roots, recursive, limit, max_depth, stats)
        except (AuthenticationError, RiskError) as exc:
            exc.stats = stats
            raise

    def _run(self, roots, recursive, limit, max_depth, stats):
        if self.stop_event.is_set():
            return stats
        queue = []
        for root in roots:
            cid = self.client.resolve_folder(root)
            folder_name = self.client.info(cid).name if cid != "0" else ""
            queue.append((cid, 0, folder_name))
        visited = set()
        while queue and not self.stop_event.is_set():
            cid, depth, folder_name = queue.pop(0)
            if cid in visited:
                continue
            visited.add(cid)
            entries = self.client.listdir(cid)
            groups = {}
            for entry in entries:
                if entry.is_dir:
                    if depth < (max_depth if recursive else 1):
                        queue.append((entry.id, depth + 1, entry.name))
                elif Path(entry.name).suffix.lower() in VIDEO_EXTS:
                    number = self.extract_number(entry.name) or self.extract_number(folder_name)
                    if number:
                        groups.setdefault(number, []).append(entry)
                    else:
                        stats["nomatch"] += 1
            for number, videos in groups.items():
                if self.stop_event.is_set() or stats["ok"] + stats["notfound"] + stats["fail"] >= limit:
                    return stats
                stats["total"] += 1
                try:
                    status = self._process(number, videos, entries)
                except (AuthenticationError, RiskError):
                    raise
                except Stopped:
                    return stats
                except Exception as exc:
                    status = "fail"
                    message = str(exc) if isinstance(exc, CloudError) else type(exc).__name__
                    stats["errors"].append(number + "：" + message)
                stats[status] += 1
        return stats

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
