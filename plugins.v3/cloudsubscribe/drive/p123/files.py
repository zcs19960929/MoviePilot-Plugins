"""123 网盘目录、查询与文件变更能力。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, List, Mapping, Optional

from app.core.cache import TTLCache
from app.log import logger

from .client import check_response, is_success
from ..common import CloudDriveFileServiceBase, create_directory_cache, safe_int
from ...core.cloud import CloudFile
from ...core.transfer import HttpFileDownloadService


def cloud_file(item: Any) -> Optional[CloudFile]:
    if not isinstance(item, Mapping):
        return None
    normalized = dict(item)
    raw = normalized.get("raw")
    raw = dict(raw) if isinstance(raw, Mapping) else normalized
    file_id = normalized.get("id") or raw.get("FileId") or raw.get("fileId")
    name = str(
        normalized.get("name") or raw.get("FileName") or raw.get("fileName") or ""
    ).strip()
    if file_id in (None, "") or not name:
        return None
    is_directory = bool(
        normalized.get("is_dir")
        or safe_int(raw.get("Type") or raw.get("type")) == 1
    )
    checksum = str(
        normalized.get("md5") or raw.get("Etag") or raw.get("etag") or ""
    ).strip()
    size = 0 if is_directory else safe_int(
        normalized.get("size") or raw.get("Size") or raw.get("size")
    )
    s3_key_flag = str(
        normalized.get("s3keyflag")
        or raw.get("S3KeyFlag")
        or raw.get("s3KeyFlag")
        or ""
    ).strip()
    playback_values = {}
    if not is_directory:
        playback_values = {
            "file_id": str(file_id),
            "md5": checksum,
            "size": str(size),
            "s3_key_flag": s3_key_flag,
        }
    return CloudFile(
        id=str(file_id),
        name=name,
        is_directory=is_directory,
        size=size,
        sha1=checksum,
        md5=checksum,
        playback_values=playback_values,
        native=raw,
    )


@dataclass
class P123FileService(CloudDriveFileServiceBase):
    client: Any
    page_size: int = 100
    root_directory_id = "0"
    provider_name = "123"
    provider_key = "p123"
    _directory_cache: TTLCache = field(init=False, repr=False)

    def __post_init__(self):
        self._directory_cache = create_directory_cache("p123", self.client)

    def download_file(self, file_item: CloudFile, local_path: str,
                      progress_callback=None, stop_requested=None,
                      preserve_partial: bool = False,
                      download_threads: int = 5) -> str:
        download_url, headers = self.resolve_download_link(file_item)
        return HttpFileDownloadService(
            lambda _: (download_url, headers), concurrency=download_threads,
        ).download_file(
            file_item, local_path, progress_callback, stop_requested,
            preserve_partial=preserve_partial,
        )

    def resolve_download_link(self, file_item: CloudFile) -> tuple[str, dict]:
        raw = file_item.native if isinstance(file_item.native, Mapping) else {}
        values = file_item.playback_values or {}
        payload = {
            "etag": str(
                raw.get("Etag") or raw.get("etag")
                or values.get("md5") or file_item.md5 or ""
            ).strip(),
            "file_id": file_item.id,
            "file_name": file_item.name,
            "s3_key_flag": str(
                raw.get("S3KeyFlag") or raw.get("s3KeyFlag")
                or values.get("s3_key_flag") or ""
            ).strip(),
            "size": int(file_item.size or raw.get("Size") or raw.get("size") or 0),
        }
        if not payload["etag"] or not payload["s3_key_flag"]:
            raise RuntimeError(f"123 文件元数据不完整，无法获取下载地址：{file_item.name}")
        response = self.client.get_download_info(payload)
        check_response(response)
        data = response.get("data") or {}
        download_url = str(
            data.get("DownloadUrl") or data.get("downloadUrl") or ""
        ).strip()
        if not download_url:
            raise RuntimeError(f"123 未返回下载地址：{file_item.name}")
        return download_url, {"Referer": "https://yun.123pan.com/"}

    def _list(self, directory_id: str) -> List[CloudFile]:
        directory_id = str(directory_id or "0")
        cached = self._directory_cache.get(directory_id)
        if cached is not None:
            return list(cached)
        result: List[CloudFile] = []
        page = 1
        next_val = 0
        while True:
            response = self.client.list_files(
                parent_id=directory_id,
                page=page,
                limit=self.page_size,
                order_by="file_id",
                order_direction="desc",
                next_val=next_val,
            )
            check_response(response)
            data = response.get("data") or {}
            items = data.get("InfoList") or data.get("infoList") or []
            if not items:
                break
            for item in items:
                file_item = cloud_file(item)
                if file_item is not None:
                    result.append(file_item)
            next_str = str(data.get("Next") or data.get("next") or "-1").strip()
            if next_str == "-1" or len(items) == 0:
                break
            next_val = next_str
            page += 1
        self._directory_cache.set(directory_id, tuple(result))
        return result

    def _create_folder(self, name: str, parent_id: str) -> Optional[CloudFile]:
        response = self.client.create_folder(name, parent_id=parent_id)
        if not is_success(response):
            return None
        self._invalidate_directory_cache()
        data = response.get("data") or {}
        return cloud_file(data.get("Info") or data.get("info") or data)

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        response = self.client.rename(item.id, target_name)
        success = is_success(response)
        if success:
            self._invalidate_directory_cache()
            if item.is_directory:
                self._invalidate_path_cache()
        return success

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> Optional[CloudFile]:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        response = self.client.move(item.id, parent_id=lookup.directory_id)
        if not is_success(response):
            return None
        self._invalidate_directory_cache()
        if item.is_directory:
            self._invalidate_path_cache()
        if target_name and target_name != item.name:
            if not self.rename_file(save_path, item, target_name):
                logger.warning(f"123网盘文件移入目录后重命名失败，保留原名：{item.name} -> {target_name}")
                return self.find_file(save_path, item.name)
        return self.find_file(save_path, target_name or item.name)

    def delete_file(self, file_id: str) -> bool:
        response = self.client.trash(file_id)
        success = is_success(response)
        if success:
            self._invalidate_directory_cache()
            self._invalidate_path_cache()
        return success

    def rename_files(self, path: str, items: dict) -> dict[str, CloudFile]:
        """批量重命名。"""
        renamed = {}
        for key, value in (items or {}).items():
            item = value.get("item")
            target_name = str(value.get("target_name") or "")
            if item and target_name:
                resp = self.client.rename(item.id, target_name)
                if is_success(resp):
                    renamed[str(key)] = replace(item, name=target_name)
        if renamed:
            self._invalidate_directory_cache()
            if any(item.is_directory for item in renamed.values()):
                self._invalidate_path_cache()
        return renamed

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]:
        """批量移动。"""
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return {}
        entries = list(dict(items or {}).items())
        moved = {}
        for offset in range(0, len(entries), 100):
            batch = entries[offset:offset + 100]
            response = self.client.move(
                [item.id for _, item in batch],
                parent_id=lookup.directory_id,
            )
            if is_success(response):
                moved.update({str(key): item for key, item in batch})
        if moved:
            self._invalidate_directory_cache()
            if any(item.is_directory for item in moved.values()):
                self._invalidate_path_cache()
        return moved

    def delete_files(self, file_ids: list[str]) -> set[str]:
        """批量删除。"""
        file_ids = list(dict.fromkeys(
            str(value) for value in (file_ids or []) if str(value or "")
        ))
        deleted = set()
        for offset in range(0, len(file_ids), 100):
            batch = file_ids[offset:offset + 100]
            response = self.client.trash(batch)
            if is_success(response):
                deleted.update(batch)
        if deleted:
            self._invalidate_directory_cache()
            self._invalidate_path_cache()
        return deleted
