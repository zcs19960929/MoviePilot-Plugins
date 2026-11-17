"""百度网盘目录、文件操作与直链。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from threading import RLock
from typing import Any, Dict, List, Optional

from app.core.cache import TTLCache
from app.log import logger

from ..common import (
    CloudDriveFileServiceBase,
    create_directory_cache,
    normalize_path,
    safe_int,
)
from ...core.cloud import CloudFile
from ...core.transfer import HttpFileDownloadService


def join_path(parent: str, name: str) -> str:
    return normalize_path(
        f"{str(parent or '/').rstrip('/')}/{str(name or '').strip()}"
    )


def cloud_file(item: Any, parent_path: str = "/") -> Optional[CloudFile]:
    """转换为统一文件模型。"""
    if not isinstance(item, dict):
        return None
    name = str(item.get("server_filename") or item.get("filename") or "").strip()
    path = str(item.get("path") or "").strip()
    if not path:
        path = join_path(parent_path, name)
    path = normalize_path(path)
    if not name:
        name = PurePosixPath(path).name
    if not name:
        return None
    is_directory = safe_int(item.get("isdir")) == 1
    fs_id = str(item.get("fs_id") or "").strip()
    playback_values = {"path": path}
    if fs_id:
        playback_values["fs_id"] = fs_id
    return CloudFile(
        id=path,
        name=name,
        is_directory=is_directory,
        size=0 if is_directory else safe_int(item.get("size")),
        md5="" if is_directory else str(item.get("md5") or ""),
        playback_values=playback_values,
        native=item,
    )


def list_data(response: Any) -> list:
    if not isinstance(response, dict):
        return []
    value = response.get("list")
    return value if isinstance(value, list) else []


def wait_task(client: Any, task_id: str, timeout: int = 120) -> bool:
    """轮询百度异步任务。"""
    task_id = str(task_id or "").strip()
    if not task_id or task_id == "0":
        return True
    deadline = time.monotonic() + max(1, int(timeout or 120))
    while time.monotonic() < deadline:
        response = client.request(
            "GET",
            "/share/taskquery",
            params={"taskid": task_id},
            with_bdstoken=True,
        )
        if not client.is_success(response):
            return False
        status = str(response.get("status") or "").strip().lower()
        if status in {"success", "finished", "done"}:
            return True
        if status in {"failed", "error", "canceled", "cancelled"}:
            return False
        time.sleep(0.5)
    logger.warning(f"百度网盘异步任务超时：{task_id}")
    return False


@dataclass
class BaiduFileService(CloudDriveFileServiceBase):
    client: Any
    page_size: int = 1000
    root_directory_id = "/"
    provider_name = "百度网盘"
    provider_key = "baidu"
    _directory_lock: RLock = field(default_factory=RLock, repr=False)
    _directory_cache: TTLCache = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._directory_cache = create_directory_cache("baidu", self.client)

    def _list(self, directory_id: str) -> List[CloudFile]:
        directory = normalize_path(directory_id or self.root_directory_id)
        cached = self._directory_cache.get(directory)
        if cached is not None:
            return list(cached)
        files: List[CloudFile] = []
        page = 1
        while True:
            response = self.client.request(
                "GET",
                "/api/list",
                params={
                    "dir": directory,
                    "order": "name",
                    "desc": 0,
                    "showempty": 1,
                    "page": page,
                    "num": self.page_size,
                },
                with_bdstoken=True,
            )
            if not self.client.is_success(response):
                raise RuntimeError(
                    response.get("message") or f"读取百度网盘目录失败：{directory}"
                )
            raw_items = list_data(response)
            files.extend(
                item for raw in raw_items if (item := cloud_file(raw, directory))
            )
            more = response.get("has_more")
            if (
                    not raw_items
                    or (more is not None and str(more).lower() in {"0", "false"})
                    or (more is None and len(raw_items) < self.page_size)
            ):
                self._directory_cache.set(directory, tuple(files))
                return files
            page += 1

    def _create_folder(self, name: str, parent_id: str) -> Optional[CloudFile]:
        path = join_path(parent_id, name)
        response = self.client.request(
            "POST",
            "/api/create",
            params={"a": "commit", "rtype": 0},
            data={"path": path, "isdir": 1, "block_list": "[]"},
            with_bdstoken=True,
        )
        self._invalidate_directory_cache()
        if not self.client.is_success(response):
            existing = next(
                (
                    item for item in self._list(parent_id)
                    if item.is_directory and item.name == name
                ),
                None,
            )
            if existing:
                return existing
            raise RuntimeError(
                response.get("message") or f"创建百度网盘目录失败：{path}"
            )
        return CloudFile(
            id=path,
            name=str(name),
            is_directory=True,
            playback_values={"path": path},
            native={"path": path, "server_filename": str(name), "isdir": 1},
        )

    def _is_success(self, response: Any) -> bool:
        return self.client.is_success(response)

    def _filemanager(
            self,
            opera: str,
            file_list: List[Any],
            *,
            new_verify: bool = False,
    ) -> Dict[str, Any]:
        data = {"filelist": json.dumps(file_list, ensure_ascii=False), "ondup": "fail"}
        if new_verify:
            data["newVerify"] = 1
        response = self.client.request(
            "POST",
            "/api/filemanager",
            params={"opera": opera, "async": 2, "onnest": "fail"},
            data=data,
            with_bdstoken=True,
        )
        if not self.client.is_success(response):
            return response
        task_id = str(response.get("taskid") or response.get("task_id") or "")
        if task_id and task_id != "0" and not wait_task(self.client, task_id):
            return {
                "errno": -1,
                "message": "百度文件任务等待超时或未成功完成，请检查网盘实际结果",
            }
        rows = response.get("info")
        failed = [
            row for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and safe_int(row.get("errno")) != 0
        ]
        if failed:
            response = dict(response)
            response["errno"] = 12
            response.setdefault(
                "message", self.client.error_message({"errno": 12})
            )
        return response

    @staticmethod
    def _failed_paths(response: Dict[str, Any]) -> set[str]:
        if safe_int(response.get("errno")) != 12:
            return set()
        failed: set[str] = set()
        for key in ("info", "list"):
            rows = response.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if safe_int(row.get("errno") or row.get("error_code")) == 0:
                    continue
                path = str(row.get("path") or row.get("from") or "").strip()
                if path:
                    failed.add(normalize_path(path))
        return failed

    def rename_file(self, path: str, item: Any, target_name: str) -> bool:
        item = self._ensure_cloud_file(item)
        target_name = str(target_name or "").strip()
        if not target_name or target_name == item.name:
            return True
        result = self._filemanager(
            "rename", [{"path": item.id, "newname": target_name}]
        )
        self._invalidate_directory_cache()
        if item.is_directory:
            self._invalidate_path_cache()
        return self._is_success(result)

    def move_file(
            self, item: Any, save_path: str, target_name: str
    ) -> Optional[CloudFile]:
        item = self._ensure_cloud_file(item)
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        final_name = str(target_name or item.name).strip() or item.name
        current_parent = str(PurePosixPath(item.id).parent)
        if current_parent == str(lookup.directory_id):
            success = self.rename_file(save_path, item, final_name)
        else:
            result = self._filemanager(
                "move",
                [{
                    "path": item.id,
                    "dest": lookup.directory_id,
                    "newname": final_name,
                    "ondup": "fail",
                }],
            )
            success = self._is_success(result)
            if success:
                self._invalidate_directory_cache()
                if item.is_directory:
                    self._invalidate_path_cache()
        return self.find_file(save_path, final_name) if success else None

    def move_files(
            self, items: Dict[str, CloudFile], save_path: str
    ) -> Dict[str, CloudFile]:
        entries = [
            (str(key), value) for key, value in dict(items or {}).items()
            if value is not None
        ]
        if not entries:
            return {}
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return {}
        moved: Dict[str, CloudFile] = {}
        for offset in range(0, len(entries), 500):
            batch = entries[offset:offset + 500]
            result = self._filemanager(
                "move",
                [
                    {
                        "path": item.id,
                        "dest": lookup.directory_id,
                        "newname": item.name,
                        "ondup": "fail",
                    }
                    for _, item in batch
                ],
            )
            failed = self._failed_paths(result)
            if not self._is_success(result) and not failed:
                failed = {normalize_path(item.id) for _, item in batch}
            for key, item in batch:
                if normalize_path(item.id) in failed:
                    continue
                new_path = join_path(lookup.directory_id, item.name)
                moved[key] = replace(
                    item,
                    id=new_path,
                    playback_values={
                        **dict(item.playback_values or {}),
                        "path": new_path,
                    },
                    native={
                        **dict(item.native or {}),
                        "path": new_path,
                    },
                )
        if moved:
            self._invalidate_directory_cache()
            if any(item.is_directory for item in moved.values()):
                self._invalidate_path_cache()
        return moved

    def delete_file(self, file_id: str) -> bool:
        return str(file_id) in self.delete_files([str(file_id)])

    def delete_files(self, file_ids: List[str]) -> set[str]:
        normalized = [
            normalize_path(value) for value in dict.fromkeys(
                str(value) for value in (file_ids or [])
                if str(value or "").strip()
            )
        ]
        if not normalized:
            return set()
        deleted: set[str] = set()
        for offset in range(0, len(normalized), 500):
            batch = normalized[offset:offset + 500]
            result = self._filemanager(
                "delete", list(batch), new_verify=True
            )
            failed = self._failed_paths(result)
            if not self._is_success(result) and not failed:
                failed = set(batch)
            deleted.update(path for path in batch if path not in failed)
        if deleted:
            self._invalidate_directory_cache()
            self._invalidate_path_cache()
        return deleted

    def resolve_download_link(
            self, file_item: CloudFile
    ) -> tuple[str, Dict[str, str]]:
        path = self._item_path(file_item)
        if not path:
            raise RuntimeError("百度网盘文件缺少网盘路径，无法获取直链")
        response = self.client.request(
            "POST",
            "/api/filemetas",
            params={"method": "filemetas", "dlink": 1, "blocks": 0},
            data={"target": json.dumps([path], ensure_ascii=False)},
            with_bdstoken=True,
        )
        if not self.client.is_success(response):
            raise RuntimeError(
                response.get("message") or f"获取百度网盘直链失败：{path}"
            )
        info = response.get("info")
        entries = info if isinstance(info, list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            dlink = str(entry.get("dlink") or "").strip()
            if dlink:
                headers = self.client.download_headers()
                if self.client.cookie:
                    headers["Cookie"] = self.client.cookie
                return dlink, headers
        raise RuntimeError("百度网盘未返回文件下载地址")

    def download_file(
            self, file_item: CloudFile, local_path: str,
            progress_callback=None, stop_requested=None,
            preserve_partial: bool = False,
            download_threads: int = 5,
    ) -> str:
        service = HttpFileDownloadService(
            self.resolve_download_link,
            concurrency=download_threads,
            part_size=10 * 1024 * 1024,
        )
        return service.download_file(
            file_item, local_path, progress_callback, stop_requested,
            preserve_partial=preserve_partial,
        )

    @staticmethod
    def _item_path(file_item: CloudFile) -> str:
        native = file_item.native if isinstance(file_item.native, dict) else {}
        for value in (
                file_item.id,
                (file_item.playback_values or {}).get("path"),
                native.get("path"),
        ):
            text = str(value or "").strip()
            if text.startswith("/"):
                return normalize_path(text)
        return ""
