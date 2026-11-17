"""百度网盘离线下载与任务管理。"""

from __future__ import annotations

import re
import time
from threading import RLock
from typing import Any, Dict, List, Optional, Set
from urllib.parse import unquote

from app.log import logger

from ..common import safe_int
from ...utils.magnet import extract_magnet_hash, parse_magnet_metadata

_ED2K_RE = re.compile(
    r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|/?", re.I
)
_MAGNET_HASH_RE = re.compile(r"(?:^|[?&])xt=urn:btih:([0-9A-Fa-f]{40})(?:&|$)", re.I)


class BaiduOfflineService:
    """百度网盘离线任务服务。"""

    CACHE_TTL = 60
    STATUS_TEXT = {
        0: "下载完成",
        1: "下载中",
        2: "系统错误",
        3: "任务不存在",
        4: "下载超时",
        5: "下载失败",
        6: "空间不足",
        7: "已取消",
    }
    STATE = {
        0: "completed",
        1: "running",
        2: "failed",
        3: "failed",
        4: "failed",
        5: "failed",
        6: "failed",
        7: "canceled",
    }

    def __init__(self, client: Any, files: Any):
        self.client = client
        self._files = files
        self._lock = RLock()
        self._tasks: List[Dict[str, Any]] = []
        self._updated_at = 0.0
        self._refresh_ok = False

    @staticmethod
    def is_ed2k_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("ed2k://")

    @staticmethod
    def is_magnet_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("magnet:?")

    @classmethod
    def is_offline_url(cls, url: str) -> bool:
        return cls.is_ed2k_url(url) or cls.is_magnet_url(url)

    @staticmethod
    def parse_ed2k_link(url: str) -> Dict[str, Any]:
        normalized = str(url or "").replace("｜", "|").strip()
        match = _ED2K_RE.fullmatch(normalized)
        if not match:
            return {}
        return {
            "url": normalized,
            "name": unquote(match.group(1)),
            "size": safe_int(match.group(2)),
            "hash": match.group(3).upper(),
        }

    def parse_magnet_link(
            self, url: str, fetch_metadata: bool = False
    ) -> Dict[str, Any]:
        metadata = parse_magnet_metadata(url, fetch_info=fetch_metadata)
        if not metadata:
            return {}
        return {
            "url": str(url).strip(),
            "name": metadata.get("display_name") or metadata["info_hash"],
            "size": safe_int(metadata.get("size")),
            "hash": str(metadata["info_hash"]).upper(),
            "metadata": metadata,
        }

    @staticmethod
    def _task_hash(url: str) -> str:
        text = str(url or "").strip()
        ed2k = _ED2K_RE.fullmatch(text)
        if ed2k:
            return ed2k.group(3).upper()
        magnet = _MAGNET_HASH_RE.search(text)
        if magnet:
            return magnet.group(1).upper()
        return str(extract_magnet_hash(text) or "").upper()

    def _format_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        native_id = str(
            task.get("task_id") or task.get("taskId") or task.get("id") or ""
        ).strip()
        status = safe_int(task.get("status"))
        state = self.STATE.get(status, "processing")
        size = safe_int(task.get("file_size") or task.get("size"))
        finished = safe_int(task.get("finished_size"))
        percent = 100.0 if state == "completed" else (
            round(finished / size * 100, 2) if size > 0 else 0.0
        )
        source_url = str(
            task.get("source_url") or task.get("source") or task.get("url") or ""
        ).strip()
        add_time = safe_int(
            task.get("create_time") or task.get("ctime") or task.get("add_time")
        )
        if add_time > 10_000_000_000:
            add_time //= 1000
        save_path = str(
            task.get("save_dir") or task.get("save_path") or task.get("path") or ""
        ).strip()
        return {
            "id": native_id,
            "native_id": native_id,
            "name": str(
                task.get("task_name") or task.get("file_name")
                or task.get("name") or source_url or "未命名任务"
            ),
            "size": size,
            "state": state,
            "completed": state == "completed",
            "failed": state == "failed",
            "status_text": self.STATUS_TEXT.get(status, "处理中"),
            "percent": max(0.0, min(float(percent), 100.0)),
            "add_time": add_time,
            "res": source_url,
            "save_path": save_path,
        }

    def _load_tasks(self, force: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            if (
                    not force
                    and self._updated_at
                    and time.time() - self._updated_at < self.CACHE_TTL
            ):
                return [dict(task) for task in self._tasks]
        try:
            response = self.client.request(
                "GET",
                "/rest/2.0/services/cloud_dl",
                params={
                    "method": "list_task",
                    "t": int(time.time() * 1000),
                    "need_task_info": 1,
                    "status": 255,
                    "start": 0,
                    "limit": 1000,
                },
                with_bdstoken=True,
            )
            if not self.client.is_success(response):
                raise RuntimeError(
                    response.get("message") or "读取百度网盘离线任务失败"
                )
            raw_tasks = response.get("task_info")
            if isinstance(raw_tasks, dict):
                rows = list(raw_tasks.values())
            elif isinstance(raw_tasks, list):
                rows = raw_tasks
            else:
                rows = []
            tasks = [
                self._format_task(item) for item in rows
                if isinstance(item, dict)
                   and str(item.get("task_id") or item.get("taskId") or item.get("id") or "").strip()
            ]
            with self._lock:
                self._tasks = tasks
                self._updated_at = time.time()
                self._refresh_ok = True
                return [dict(task) for task in tasks]
        except Exception as error:
            logger.warning(f"读取百度网盘离线任务失败，继续使用缓存：{error}")
            with self._lock:
                self._refresh_ok = False
                return [dict(task) for task in self._tasks]

    def get_offline_task_list_snapshot(
            self,
            force: bool = False,
            task_ids: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        tasks = self._load_tasks(force=force)
        normalized = {
            str(value or "").strip().upper()
            for value in (task_ids or set())
            if str(value or "").strip()
        }
        if normalized:
            tasks = [
                task for task in tasks
                if str(task.get("id") or "").strip().upper() in normalized
            ]
        with self._lock:
            return {
                "tasks": tasks,
                "updated_at": self._updated_at,
                "cache_ttl": self.CACHE_TTL,
                "refresh_ok": self._refresh_ok,
            }

    def get_offline_tasks_snapshot(self, force: bool = False) -> Dict[str, Any]:
        snapshot = self.get_offline_task_list_snapshot(force=force)
        snapshot["quota"] = {}
        return snapshot

    def _find_task(self, task_id: str) -> Dict[str, Any]:
        normalized = str(task_id or "").strip().upper()
        if not normalized:
            raise ValueError("离线任务ID不能为空")
        for task in self._load_tasks(force=False):
            if str(task.get("id") or "").strip().upper() == normalized:
                return task
        return {}

    def _add_task(self, url: str, save_path: str) -> Any:
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"添加百度网盘离线下载失败：目标目录不可用 {save_path}")
            return False
        response = self.client.request(
            "POST",
            "/rest/2.0/services/cloud_dl",
            params={"method": "add_task", "t": int(time.time() * 1000)},
            data={
                "source_url": str(url).strip(),
                "save_path": str(lookup.directory_id),
                "type": 3,
            },
            with_bdstoken=True,
        )
        if not self.client.is_success(response):
            logger.error(
                f"添加百度网盘离线下载失败："
                f"{response.get('message') or response.get('errno')}"
            )
            return False
        with self._lock:
            self._updated_at = 0
        return str(response.get("task_id") or response.get("taskid") or "").strip() or True

    def add_offline_download(self, url: str, save_path: str, **kwargs: Any) -> Any:
        """复用或提交离线任务。"""
        if not self.is_offline_url(url):
            return False
        target_hash = self._task_hash(url)
        for task in self._load_tasks(force=True):
            if task.get("failed"):
                continue
            if target_hash and self._task_hash(str(task.get("res") or "")) != target_hash:
                continue
            logger.info(
                f"百度网盘离线任务已存在，复用任务："
                f"{task.get('name') or url}"
            )
            return str(task.get("id") or "").strip() or True
        return self._add_task(url, save_path)

    def restart_offline_task(self, task_id: str) -> bool:
        """删除后重新提交。"""
        task = self._find_task(task_id)
        if not task:
            logger.warning(f"百度网盘离线任务不存在：{task_id}")
            return False
        if task.get("state") in {"running", "completed"}:
            return True
        source = str(task.get("res") or "").strip()
        save_path = str(task.get("save_path") or "/").strip() or "/"
        if not source:
            logger.warning(f"百度网盘离线任务缺少原始链接，无法重试：{task_id}")
            return False
        if not self._delete_tasks([str(task.get("id") or task_id)]):
            return False
        return bool(self._add_task(source, save_path))

    def _delete_tasks(self, task_ids: List[str]) -> List[str]:
        deleted: List[str] = []
        for task_id in task_ids:
            response = self.client.request(
                "POST",
                "/rest/2.0/services/cloud_dl",
                params={"method": "delete_task", "t": int(time.time() * 1000)},
                data={"task_id": str(task_id)},
                with_bdstoken=True,
            )
            if not self.client.is_success(response):
                logger.warning(
                    f"删除百度网盘离线任务失败：{task_id} "
                    f"{response.get('message') or response.get('errno')}"
                )
                continue
            deleted.append(str(task_id))
        with self._lock:
            removed = {value.upper() for value in deleted}
            self._tasks = [
                task for task in self._tasks
                if str(task.get("id") or "").strip().upper() not in removed
            ]
            self._updated_at = time.time()
        return deleted

    def delete_offline_task(
            self, task_id: str, delete_source_file: bool = False
    ) -> bool:
        return self.delete_offline_tasks(
            [task_id], delete_source_file=delete_source_file
        ) > 0

    def delete_offline_tasks(
            self, task_ids: List[str], delete_source_file: bool = False
    ) -> int:
        normalized = list(dict.fromkeys(
            str(value or "").strip() for value in (task_ids or [])
            if str(value or "").strip()
        ))
        if not normalized:
            raise ValueError("请选择需要删除的离线任务")
        return len(self._delete_tasks(normalized))
