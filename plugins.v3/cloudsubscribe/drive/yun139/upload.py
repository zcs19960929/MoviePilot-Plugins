"""移动云盘 SHA-256 秒传与分片上传。"""

from __future__ import annotations

import hashlib
import mimetypes
import time
from pathlib import Path

DEFAULT_PART_SIZE = 100 * 1024 * 1024
LARGE_PART_SIZE = 512 * 1024 * 1024
LARGE_FILE_THRESHOLD = 30 * 1024 * 1024 * 1024
MAX_PARTS_PER_REQUEST = 100


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parts(size: int) -> list[dict]:
    """按文件大小切分分片，超过 30GiB 时改用 512MiB 分片。"""
    part_size = LARGE_PART_SIZE if size > LARGE_FILE_THRESHOLD else DEFAULT_PART_SIZE
    count = max(1, (max(0, size) + part_size - 1) // part_size)
    parts = []
    for index in range(count):
        offset = index * part_size
        parts.append({
            "partNumber": index + 1,
            "partSize": min(part_size, max(0, size - offset)),
            "parallelHashCtx": {"partOffset": offset},
        })
    return parts


class Yun139UploadService:
    """实现本地文件上传与 SHA-256 秒传探测。"""

    #: 秒传探测依赖本地文件计算 SHA-256。
    rapid_requires_local_file = True

    def __init__(self, client, files):
        self.client = client
        self.files = files

    def _resolve_parent(self, save_path: str) -> str:
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            raise RuntimeError(f"移动云盘目标目录不可用：{save_path}")
        return lookup.directory_id

    @staticmethod
    def _content_type(name: str) -> str:
        guessed, _ = mimetypes.guess_type(name)
        return guessed or "application/octet-stream"

    def _create_upload(
            self,
            parent_id: str,
            name: str,
            size: int,
            checksum: str,
            parts: list[dict],
    ) -> dict:
        return self.client.request("/file/create", {
            "contentHash": checksum.lower(),
            "contentHashAlgorithm": "SHA256",
            "contentType": self._content_type(name),
            "fileRenameMode": "auto_rename",
            "name": name,
            "parallelUpload": False,
            "parentFileId": parent_id,
            "partInfos": parts[:MAX_PARTS_PER_REQUEST],
            "size": size,
            "type": "file",
        })

    def _wait_for_file(self, save_path: str, name: str) -> bool:
        for index in range(10):
            if self.files.find_file(save_path, name):
                return True
            if index < 9:
                time.sleep(0.5)
        return False

    def try_rapid_upload(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int,
    ) -> bool:
        """按 SHA-256 探测秒传；未命中返回 False 由调用方回退普通上传。"""
        if str(algorithm or "").lower() != "sha256":
            return False
        path = Path(local_path)
        if int(size or 0) <= 0 or not path.is_file():
            return False
        name = target_name or path.name
        data = self._create_upload(
            self._resolve_parent(save_path),
            name,
            int(size),
            checksum,
            build_parts(int(size)),
        )
        if not (bool(data.get("rapidUpload")) or bool(data.get("exist"))):
            return False
        final_name = str(data.get("fileName") or name)
        if not self._wait_for_file(save_path, final_name):
            raise RuntimeError("移动云盘秒传完成后未找到目标文件")
        return True

    def upload_file(
            self, local_path: str, save_path: str, target_name: str = "",
            file_sha1: str = "", progress_callback=None,
    ) -> bool:
        """分片上传本地文件；命中秒传时直接返回。"""
        source = Path(local_path)
        if not source.is_file():
            return False
        parent_id = self._resolve_parent(save_path)
        size = source.stat().st_size
        name = target_name or source.name
        checksum = file_sha256(source)
        parts = build_parts(size)
        created = self._create_upload(parent_id, name, size, checksum, parts)
        final_name = str(created.get("fileName") or name)
        if bool(created.get("rapidUpload")) or bool(created.get("exist")):
            if progress_callback:
                progress_callback(size, size)
            return self.files.find_file(save_path, final_name) is not None
        file_id = str(created.get("fileId") or "").strip()
        upload_id = str(created.get("uploadId") or "").strip()
        if not file_id or not upload_id:
            raise RuntimeError("移动云盘上传初始化未返回 fileId 或 uploadId")

        urls = {
            int(item.get("partNumber") or 0): str(item.get("uploadUrl") or "")
            for item in created.get("partInfos") or []
            if isinstance(item, dict) and item.get("uploadUrl")
        }
        uploaded = 0
        with source.open("rb") as handle:
            for part in parts:
                number = int(part["partNumber"])
                upload_url = urls.get(number) or self._fetch_upload_url(
                    file_id, upload_id, part, urls
                )
                if not upload_url:
                    raise RuntimeError(f"移动云盘第 {number} 个分片未返回上传地址")
                handle.seek(int(part["parallelHashCtx"]["partOffset"]))
                chunk = handle.read(int(part["partSize"]))
                self.client.put_part(upload_url, chunk)
                uploaded += len(chunk)
                if progress_callback:
                    progress_callback(min(uploaded, size), size)
        self.client.request("/file/complete", {
            "contentHash": checksum.lower(),
            "contentHashAlgorithm": "SHA256",
            "fileId": file_id,
            "uploadId": upload_id,
        })
        return self.files.find_file(save_path, final_name) is not None

    def _fetch_upload_url(
            self, file_id: str, upload_id: str, part: dict, urls: dict[int, str]
    ) -> str:
        """超过 100 分片时按需补齐上传地址。"""
        data = self.client.request("/file/getUploadUrl", {
            "fileId": file_id,
            "uploadId": upload_id,
            "partInfos": [part],
            "commonAccountInfo": {
                "account": self.client.account,
                "accountType": 1,
            },
        })
        for item in data.get("partInfos") or []:
            if not isinstance(item, dict):
                continue
            number = int(item.get("partNumber") or 0)
            url = str(item.get("uploadUrl") or "").strip()
            if number and url:
                urls[number] = url
        return urls.get(int(part["partNumber"]), "")
