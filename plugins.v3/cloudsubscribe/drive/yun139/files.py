"""移动云盘目录读取、文件查询与文件变更。"""

import time
from pathlib import PurePosixPath

from app.core.cache import TTLCache
from app.log import logger

from ..common import (
    CloudDriveFileServiceBase,
    create_directory_cache,
    create_directory_path_cache,
)
from ...core.cloud import CloudFile, DirectoryListing
from ...core.transfer import HttpFileDownloadService

ROOT_ID = "/"
LIST_PAGE_SIZE = 100
BATCH_LIMIT = 100


class Yun139FileService(CloudDriveFileServiceBase):
    """适配移动云盘个人云的文件接口。"""

    provider_name = "移动云盘"
    root_directory_id = ROOT_ID

    def __init__(self, client):
        self.client = client
        self._directory_path_cache = create_directory_path_cache(
            "yun139", client, ROOT_ID
        )
        self._directory_cache: TTLCache = create_directory_cache("yun139", client)

    @staticmethod
    def _to_cloud_file(entry: dict) -> CloudFile | None:
        if not isinstance(entry, dict):
            return None
        file_id = str(entry.get("fileId") or entry.get("catalogId") or "").strip()
        name = str(entry.get("name") or entry.get("catalogName") or "").strip()
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return CloudFile(
            file_id,
            name,
            str(entry.get("type") or "").strip().lower() == "folder",
            size=size,
            native=entry,
        )

    def _list(self, directory_id: str) -> list[CloudFile]:
        parent = str(directory_id or ROOT_ID) or ROOT_ID
        items: list[CloudFile] = []
        cursor = ""
        while True:
            data = self.client.request("/file/list", {
                "imageThumbnailStyleList": ["Small", "Large"],
                "orderBy": "updated_at",
                "orderDirection": "DESC",
                "pageInfo": {"pageCursor": cursor, "pageSize": LIST_PAGE_SIZE},
                "parentFileId": parent,
            })
            for entry in data.get("items") or []:
                item = self._to_cloud_file(entry)
                if item and item.id and item.name:
                    items.append(item)
            cursor = str(data.get("nextPageCursor") or "").strip()
            if not cursor:
                break
        return items

    def list_directory(self, directory_id: str) -> DirectoryListing:
        directory_id = str(directory_id or ROOT_ID) or ROOT_ID
        cached = self._directory_cache.get(directory_id)
        if cached is not None:
            return cached
        listing = DirectoryListing(True, tuple(self._list(directory_id)))
        self._directory_cache.set(directory_id, listing)
        return listing

    def list_directories(self, path: str) -> list[dict[str, str]]:
        lookup = self.resolve_directory(path)
        if not lookup.checked or lookup.directory_id is None:
            return []
        base = PurePosixPath("/" + str(path or "/").strip("/"))
        return [
            {"id": item.id, "name": item.name, "path": str(base / item.name)}
            for item in self.list_directory(lookup.directory_id).files
            if item.is_directory
        ]

    def _create_folder(self, name: str, parent_id: str) -> CloudFile | None:
        data = self.client.request("/file/create", {
            "parentFileId": str(parent_id or ROOT_ID) or ROOT_ID,
            "name": name,
            "description": "",
            "type": "folder",
            "fileRenameMode": "force_rename",
        })
        self._invalidate_directory_cache()
        folder_id = str(data.get("fileId") or data.get("catalogId") or "").strip()
        if not folder_id:
            return None
        return CloudFile(folder_id, str(data.get("name") or name), True, native=data)

    def resolve_download_link(self, file_item: CloudFile) -> tuple[str, dict]:
        """取下载直链；CDN 开关关闭时回落到源站地址。"""
        data = self.client.request("/file/getDownloadUrl", {"fileId": file_item.id})
        cdn_url = str(data.get("cdnUrl") or "").strip()
        url = cdn_url if cdn_url and bool(data.get("cdnSwitch")) else ""
        url = url or str(data.get("url") or "").strip()
        if not url:
            raise RuntimeError("移动云盘未返回下载链接")
        headers = {
            "User-Agent": self.client.USER_AGENT,
            "Referer": self.client.WEB_ORIGIN + "/",
            "Origin": self.client.WEB_ORIGIN,
        }
        return url, headers

    def download_file(
            self, file_item: CloudFile, local_path: str,
            progress_callback=None, stop_requested=None,
            preserve_partial: bool = False,
            download_threads: int = 5,
    ) -> str:
        url, headers = self.resolve_download_link(file_item)
        return HttpFileDownloadService(
            lambda _: (url, headers), concurrency=download_threads,
        ).download_file(
            file_item, local_path, progress_callback, stop_requested,
            preserve_partial=preserve_partial,
        )

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        self.client.request("/file/update", {
            "fileId": item.id,
            "name": target_name,
            "description": "",
        })
        self._invalidate_directory_cache()
        if item.is_directory:
            self._directory_path_cache.clear()
        return self._find_with_retry(path, target_name) is not None

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> CloudFile | None:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        self.client.request("/file/batchMove", {
            "fileIds": [item.id],
            "toParentFileId": lookup.directory_id,
        })
        self._invalidate_directory_cache()
        if item.is_directory:
            self._directory_path_cache.clear()
        if target_name and target_name != item.name:
            try:
                self.client.request("/file/update", {
                    "fileId": item.id,
                    "name": target_name,
                    "description": "",
                })
            except Exception as error:
                logger.warning(
                    f"移动云盘文件移入目录后重命名失败，保留原名："
                    f"{item.name} -> {target_name}，{error}"
                )
                return self._find_with_retry(save_path, item.name)
        return self.find_file(save_path, target_name or item.name)

    def copy_file(self, item: CloudFile, save_path: str) -> bool:
        """复制到目标目录；移动云盘原生支持批量复制。"""
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return False
        self.client.request("/file/batchCopy", {
            "fileIds": [item.id],
            "toParentFileId": lookup.directory_id,
        })
        self._invalidate_directory_cache()
        return True

    def delete_file(self, file_id: str) -> bool:
        self.client.request(
            "/recyclebin/batchTrash", {"fileIds": [str(file_id or "")]}
        )
        self._invalidate_directory_cache()
        return True

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]:
        """移动云盘 batchMove 一次最多处理 100 项。"""
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return {}
        entries = [(str(key), item) for key, item in dict(items or {}).items() if item]
        moved: dict[str, CloudFile] = {}
        for offset in range(0, len(entries), BATCH_LIMIT):
            batch = entries[offset:offset + BATCH_LIMIT]
            try:
                self.client.request("/file/batchMove", {
                    "fileIds": [item.id for _, item in batch],
                    "toParentFileId": lookup.directory_id,
                })
            except Exception as error:
                logger.warning(f"移动云盘批量移动失败：{error}")
                continue
            moved.update({key: item for key, item in batch})
        if moved:
            self._invalidate_directory_cache()
            if any(item.is_directory for item in moved.values()):
                self._directory_path_cache.clear()
        return moved

    def delete_files(self, file_ids: list[str]) -> set[str]:
        """移动云盘批量回收站删除一次最多处理 100 项。"""
        unique = list(dict.fromkeys(
            str(value) for value in (file_ids or []) if str(value or "").strip()
        ))
        deleted: set[str] = set()
        for offset in range(0, len(unique), BATCH_LIMIT):
            batch = unique[offset:offset + BATCH_LIMIT]
            try:
                self.client.request("/recyclebin/batchTrash", {"fileIds": batch})
            except Exception as error:
                logger.warning(f"移动云盘批量删除失败：{error}")
                continue
            deleted.update(batch)
        if deleted:
            self._invalidate_directory_cache()
            self._directory_path_cache.clear()
        return deleted

    def _find_with_retry(self, path: str, file_name: str) -> CloudFile | None:
        """变更后目录缓存可能仍是旧数据，短暂重试直到可见。"""
        for index in range(10):
            self._invalidate_directory_cache()
            if item := self.find_file(path, file_name):
                return item
            if index < 9:
                time.sleep(0.5)
        return None
