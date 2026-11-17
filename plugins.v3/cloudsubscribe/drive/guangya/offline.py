"""光鸭网盘离线下载与任务管理能力。"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Sequence, Set
from urllib.parse import unquote

from app.log import logger

from ..common import safe_int
from ...utils.magnet import extract_magnet_hash, parse_magnet_metadata

_ED2K_RE = re.compile(
    r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|/?", re.I
)


class GuangyaOfflineService:
    """封装光鸭云添加/离线下载接口与任务列表管理。"""

    CACHE_TTL = 60

    STATUS_TEXT = {
        0: "排队中",
        1: "进行中",
        2: "已完成",
        3: "已失败",
        4: "已取消",
        5: "部分已完成",
    }

    def __init__(self, client: Any, files: Any):
        self.client = client
        self._files = files
        self._lock = RLock()
        self._tasks: List[Dict[str, Any]] = []
        self._updated_at = 0.0
        self._refresh_ok = False

    def create_cloud_task(
            self,
            url: str,
            parent_id: str = "",
            new_name: str = "",
            file_indexes: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """向光鸭提交云添加离线任务。"""
        payload: Dict[str, Any] = {
            "url": url,
            "parentId": parent_id or "",
        }
        if new_name:
            payload["newName"] = new_name
        if file_indexes is not None:
            payload["fileIndexes"] = file_indexes
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/cloudcollection/v1/create_task",
            json_data=payload,
        )

    def resolve_cloud_url(self, url: str) -> Dict[str, Any]:
        """解析 Magnet / ED2K 离线资源信息。"""
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/cloudcollection/v1/resolve_res",
            json_data={"url": url},
        )

    def cloud_task_list(
            self,
            page: int = 0,
            page_size: int = 100,
            status: Optional[List[int]] = None,
            task_ids: Optional[Sequence[str]] = None,
            cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """查询光鸭离线任务列表或指定任务详情。"""
        json_data: Dict[str, Any] = {}
        if task_ids:
            json_data["taskIds"] = [str(x).strip() for x in task_ids if str(x).strip()]
        else:
            json_data["pageSize"] = max(1, int(page_size or 100))
            json_data["cursor"] = str(cursor or "")
            if status is not None:
                json_data["status"] = status
            if page > 0:
                json_data["page"] = page
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/cloudcollection/v1/list_task",
            json_data=json_data,
        )

    def resolve_torrent(self, torrent_data_or_path: Any) -> Dict[str, Any]:
        """解析 .torrent 种子文件（multipart/form-data 调用 /cloudcollection/v1/resolve_torrent）。"""
        if isinstance(torrent_data_or_path, (str, Path)):
            with open(torrent_data_or_path, "rb") as f:
                content = f.read()
        elif isinstance(torrent_data_or_path, bytes):
            content = torrent_data_or_path
        else:
            return {}

        headers = self.client._common_headers()
        if self.client.access_token:
            headers["authorization"] = f"Bearer {self.client.access_token}"
        headers.pop("content-type", None)
        files = {"torrent": ("task.torrent", content, "application/x-bittorrent")}
        try:
            response = self.client.rate_limiter.call(
                self.client._session.post,
                f"{self.client.API_BASE_URL}/cloudcollection/v1/resolve_torrent",
                headers=headers,
                files=files,
                timeout=self.client._timeout,
            )
            return response.json() if response.text else {}
        except Exception as error:
            logger.debug(f"光鸭种子解析网络异常：{error}")
            return {}

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        """查询单个离线任务详情。"""
        return self.cloud_task_list(task_ids=[str(task_id).strip()])

    @staticmethod
    def is_ed2k_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("ed2k://")

    @staticmethod
    def is_magnet_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("magnet:?")

    @staticmethod
    def is_torrent_url(url: str) -> bool:
        return isinstance(url, (str, Path)) and (
                str(url).lstrip().lower().endswith(".torrent")
                or str(url).lstrip().lower().startswith("torrent://")
        )

    @classmethod
    def is_offline_url(cls, url: str) -> bool:
        return cls.is_ed2k_url(url) or cls.is_magnet_url(url) or cls.is_torrent_url(url)

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

    def _format_task(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """格式化光鸭任务对象，直接使用原生 taskId。"""
        task_id = str(item.get("taskId") or item.get("task_id") or item.get("id") or "").strip()
        status = safe_int(item.get("status") if item.get("status") is not None else item.get("taskStatus"))

        completed = status in (2, 5)
        failed = status in (3, 4)
        state = "completed" if completed else "failed" if failed else "running" if status == 1 else "queued"

        progress = float(item.get("progress") or item.get("percent") or 0)
        percent = 100.0 if completed else max(0.0, min(progress, 100.0))

        create_time = safe_int(item.get("createTime") or item.get("create_time") or item.get("add_time"))
        if create_time > 10_000_000_000:
            create_time //= 1000

        return {
            "id": task_id,
            "native_id": task_id,
            "name": str(item.get("fileName") or item.get("file_name") or item.get("name") or "未命名任务"),
            "size": safe_int(item.get("totalSize") or item.get("fileSize") or item.get("size")),
            "state": state,
            "completed": completed,
            "failed": failed,
            "status_text": self.STATUS_TEXT.get(status, "处理中"),
            "percent": percent,
            "add_time": create_time,
            "file_id": str(item.get("fileId") or item.get("file_id") or "").strip(),
            "parent_id": str(item.get("parentId") or item.get("parent_id") or "").strip(),
            "is_dir": bool(item.get("isDir", False)),
            "res": str(item.get("res") or item.get("url") or "").strip(),
        }

    def _load_tasks(self, force: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            if not force and self._updated_at and time.time() - self._updated_at < self.CACHE_TTL:
                return [dict(t) for t in self._tasks]

        try:
            tasks: List[Dict[str, Any]] = []
            cursor = ""
            for _ in range(3):
                response = self.cloud_task_list(page_size=100, cursor=cursor)
                if not self.client.is_success(response):
                    if not tasks:
                        raise RuntimeError(response.get("msg") or response.get("message") or "读取光鸭离线任务失败")
                    break
                data = response.get("data") or {}
                raw_list = data.get("list") or []
                for item in raw_list:
                    if isinstance(item, dict):
                        tasks.append(self._format_task(item))
                cursor = str(data.get("cursor") or "").strip()
                if not cursor or not data.get("hasMore", True):
                    break
            with self._lock:
                self._tasks = tasks
                self._updated_at = time.time()
                self._refresh_ok = True
                return [dict(t) for t in tasks]
        except Exception as error:
            logger.warning(f"读取光鸭离线任务失败，继续使用缓存：{error}")
            with self._lock:
                self._refresh_ok = False
                return [dict(t) for t in self._tasks]

    def get_offline_task_list_snapshot(
            self,
            force: bool = False,
            task_ids: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """读取离线任务列表快照。"""
        tasks = self._load_tasks(force=force)
        if task_ids:
            normalized_ids = {str(x).strip() for x in task_ids if str(x).strip()}
            tasks = [t for t in tasks if str(t.get("id")) in normalized_ids]
        with self._lock:
            return {
                "tasks": tasks,
                "updated_at": self._updated_at,
                "cache_ttl": self.CACHE_TTL,
                "refresh_ok": self._refresh_ok,
            }

    def get_offline_quota(self, force: bool = False) -> Dict[str, Any]:
        """读取光鸭离线下载配额（与 115 额度结构一致，cloudAddFiles 为每日总数）。"""
        if not self.client:
            return {}
        try:
            # 1. 检查会员身份 (vipStatus / svipStatus == 2)
            assets = self.client.data(self.client.get_assets()) if hasattr(self.client, "get_assets") else {}
            is_vip = 2 in (safe_int(assets.get("vipStatus")), safe_int(assets.get("svipStatus")))

            # 2. 读取全局权益配置
            cfg = self.client.get_global_config() if hasattr(self.client, "get_global_config") else {}
            rights = (cfg.get("rights") or {}) if isinstance(cfg, dict) else {}
            tier_rights = (rights.get("vip") if is_vip else rights.get("nonmember")) or {}
            total_limit = safe_int(tier_rights.get("cloudAddFiles") or (1000 if is_vip else 3))
            big_file_size = safe_int(tier_rights.get("bigFileSize") or (1099511627776 if is_vip else 107374182400))
            max_size_gb = big_file_size // (1024 ** 3) if big_file_size else (1024 if is_vip else 100)

            # 3. 统计今日已用任务数
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            used_today = sum(
                1 for t in (self._tasks or [])
                if safe_int(t.get("add_time")) >= today_start
            )
            remaining = max(0, total_limit - used_today)

            return {
                "total": total_limit,
                "used": used_today,
                "remaining": remaining,
                "max_size_gb": max_size_gb,
            }
        except Exception as error:
            logger.debug(f"读取光鸭离线下载额度失败：{error}")
            return {}

    def get_offline_tasks_snapshot(self, force: bool = False) -> Dict[str, Any]:
        """读取任务列表快照与离线额度（供前端 OfflineTasksDialog 渲染）。"""
        snapshot = self.get_offline_task_list_snapshot(force=force)
        snapshot["quota"] = self.get_offline_quota(force=force)
        return snapshot

    def delete_offline_task(
            self, task_id: str, delete_source_file: bool = False
    ) -> bool:
        """删除单个离线任务。"""
        return bool(self.delete_offline_tasks([task_id], delete_source_file=delete_source_file))

    def delete_offline_tasks(
            self, task_ids: List[str], delete_source_file: bool = False
    ) -> int:
        """批量删除离线任务（调用 /cloudcollection/v2/delete_task）。"""
        ids = list(dict.fromkeys(str(x).strip() for x in (task_ids or []) if str(x).strip()))
        if not ids:
            raise ValueError("请选择需要删除的离线任务")

        response = self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/cloudcollection/v2/delete_task",
            json_data={
                "taskIds": ids,
                "deleteFile": bool(delete_source_file),
            },
        )
        if not self.client.is_success(response):
            logger.warning(f"调用光鸭删除任务接口失败：{response.get('msg') or response}")

        removed = set(ids)
        with self._lock:
            self._tasks = [t for t in self._tasks if str(t.get("id")) not in removed]
            self._updated_at = time.time()
        return len(ids)

    def restart_offline_task(self, task_id: str) -> bool:
        """重试失败的离线任务（调用 /cloudcollection/v2/retry_task）。"""
        tid = str(task_id or "").strip()
        response = self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/cloudcollection/v2/retry_task",
            json_data={"taskIds": [tid]},
        )
        if not self.client.is_success(response):
            logger.warning(f"重试光鸭离线任务失败：{response.get('msg') or response}")
        with self._lock:
            self._updated_at = 0
        return True

    def add_offline_download(self, url: str, save_path: str, **kwargs: Any) -> Any:
        """
        提交离线下载到指定目录（Magnet / ED2K / .torrent）。

        返回已存在或新建任务的 taskId（str），失败返回 False。
        光鸭不对同 info hash 去重，故先比对远端任务列表复用，避免重复建单。
        """
        if not self.is_offline_url(url):
            return False
        if not self.client.access_token:
            logger.error("添加光鸭离线下载失败：账号未登录")
            return False

        # 若为种子文件，自动调用 resolve_torrent 解析出 infoHash 并转换为 Magnet
        if self.is_torrent_url(url) or (isinstance(url, str) and url.endswith(".torrent")):
            try:
                resolved = self.resolve_torrent(url)
                info = (resolved.get("data") or {}).get("btResInfo") or {}
                info_hash = info.get("infoHash")
                if info_hash:
                    url = f"magnet:?xt=urn:btih:{info_hash}"
            except Exception as error:
                logger.debug(f"光鸭种子解析失败：{error}")

        target_hash = (extract_magnet_hash(url) or "").upper()
        for task in self._load_tasks(force=True):
            task_hash = (extract_magnet_hash(str(task.get("res") or "")) or "").upper()
            if not target_hash or task_hash != target_hash:
                continue
            task_id = str(task.get("id") or task.get("native_id") or "").strip()
            if task.get("failed"):
                break
            state_text = "已完成" if task.get("completed") else "进行中"
            logger.info(
                f"光鸭离线任务已存在（{state_text}），复用任务："
                f"{task.get('name') or url}"
            )
            return task_id or True

        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"添加光鸭离线下载失败：无法获取或创建目标目录 {save_path}")
            return False
        target_name = str(kwargs.get("target_name") or "").strip()
        created = self.create_cloud_task(url, lookup.directory_id, new_name=target_name)
        if not self.client.is_success(created):
            logger.error(f"添加光鸭离线下载失败：{created.get('msg') or '任务创建失败'}")
            return False
        logger.info(f"已提交光鸭离线下载：{url} -> {save_path}")
        with self._lock:
            self._updated_at = 0
        task_id = str((created.get("data") or {}).get("taskId") or "").strip()
        return task_id or True
