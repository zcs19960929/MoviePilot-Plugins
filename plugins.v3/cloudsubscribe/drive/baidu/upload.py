"""百度网盘文件上传。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from hashlib import md5
from pathlib import Path
from typing import Any, List

from app.log import logger

from .files import BaiduFileService, join_path
from ..common import safe_int

BLOCK_SIZE_SMALL = 4 * 1024 * 1024
BLOCK_SIZE_MEDIUM = 16 * 1024 * 1024
BLOCK_SIZE_LARGE = 64 * 1024 * 1024


@dataclass
class BaiduUploadService:
    """百度网盘上传服务。"""

    client: Any
    files: BaiduFileService

    @staticmethod
    def _block_size(size: int) -> int:
        if size >= 32 * 1024 * 1024 * 1024:
            return BLOCK_SIZE_LARGE
        if size >= 8 * 1024 * 1024 * 1024:
            return BLOCK_SIZE_MEDIUM
        return BLOCK_SIZE_SMALL

    @staticmethod
    def _read_blocks(source: Path, block_size: int) -> List[tuple]:
        blocks: List[tuple] = []
        with source.open("rb") as handle:
            while True:
                chunk = handle.read(block_size)
                if not chunk:
                    break
                blocks.append((md5(chunk).hexdigest(), len(chunk)))
        return blocks

    def _locate_upload_server(self, path: str, upload_id: str) -> str:
        response = self.client.request(
            "GET",
            "/rest/2.0/pcs/file",
            params={
                "method": "locateupload",
                "path": path,
                "uploadid": upload_id,
                "upload_version": "2.0",
            },
            base_url=self.client.PCS_URL,
        )
        if self.client.is_success(response):
            servers = response.get("server")
            if isinstance(servers, list) and servers:
                server = str(servers[0]).strip()
                if server and not server.startswith(("http://", "https://")):
                    server = f"https://{server}"
                return server
        return self.client.PCS_URL

    def _upload_block(
            self,
            server: str,
            path: str,
            upload_id: str,
            partseq: int,
            chunk: bytes,
    ):
        base = str(server or self.client.PCS_URL).rstrip("/")
        return self.client.request(
            "POST",
            f"{base}/rest/2.0/pcs/superfile2",
            params={
                "method": "upload",
                "path": path,
                "uploadid": upload_id,
                "uploadsign": 0,
                "partseq": partseq,
            },
            files={"file": ("blob", chunk, "application/octet-stream")},
            timeout=max(self.client.request_timeout, 300),
            base_url=base,
        )

    def upload_file(
            self,
            local_path: str,
            save_path: str,
            target_name: str = "",
            file_sha1: str = "",
            progress_callback=None,
    ) -> bool:
        source = Path(str(local_path or ""))
        if not source.is_file():
            logger.error(f"百度网盘本地上传文件不存在：{source}")
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"百度网盘本地上传目录不可用：{save_path}")
            return False
        upload_name = str(target_name or source.name).strip()
        if not upload_name:
            logger.error("百度网盘本地上传缺少目标文件名")
            return False
        target_path = join_path(lookup.directory_id, upload_name)
        if self.files.find_file(save_path, upload_name):
            logger.info(f"百度网盘目标文件已存在，跳过上传：{target_path}")
            return True
        try:
            size = source.stat().st_size
            block_size = self._block_size(size)
            blocks = self._read_blocks(source, block_size)
            block_list = [item[0] for item in blocks]
            mtime = int(source.stat().st_mtime)
            target_dir = str(lookup.directory_id).rstrip("/") + "/"
            pre_response = self.client.request(
                "POST",
                "/api/precreate",
                params={"rtype": 0},
                data={
                    "path": target_path,
                    "size": size,
                    "isdir": 0,
                    "block_list": json.dumps(block_list, separators=(",", ":")),
                    "autoinit": 1,
                    "target_path": target_dir,
                    "local_mtime": mtime,
                },
                with_bdstoken=True,
            )
            if not self.client.is_success(pre_response):
                raise RuntimeError(
                    pre_response.get("message") or "百度网盘上传预检失败"
                )
            upload_id = str(pre_response.get("uploadid") or "").strip()
            if not upload_id:
                raise RuntimeError("百度网盘上传预检未返回 uploadid")
            server = self._locate_upload_server(target_path, upload_id)
            transferred = 0
            uploaded_blocks: List[str] = []
            with source.open("rb") as handle:
                for index, (local_md5, _length) in enumerate(blocks):
                    chunk = handle.read(block_size)
                    if not chunk:
                        break
                    response = self._upload_block(
                        server, target_path, upload_id, index, chunk
                    )
                    if not self.client.is_success(response):
                        raise RuntimeError(
                            response.get("message")
                            or f"百度网盘第 {index + 1} 个分片上传失败"
                        )
                    remote_md5 = str(response.get("md5") or "").strip().lower()
                    if remote_md5 and remote_md5 != str(local_md5).lower():
                        raise RuntimeError(
                            f"百度网盘第 {index + 1} 个分片校验失败"
                        )
                    uploaded_blocks.append(remote_md5 or local_md5)
                    transferred += len(chunk)
                    if progress_callback:
                        progress_callback(min(transferred, size), size)
            create_response = self.client.request(
                "POST",
                "/api/create",
                params={"isdir": 0, "rtype": 0},
                data={
                    "path": target_path,
                    "size": size,
                    "uploadid": upload_id,
                    "block_list": json.dumps(
                        uploaded_blocks, separators=(",", ":")
                    ),
                    "target_path": target_dir,
                    "local_mtime": mtime,
                },
                with_bdstoken=True,
            )
            if (
                    not self.client.is_success(create_response)
                    and safe_int(create_response.get("errno")) != 404
            ):
                raise RuntimeError(
                    create_response.get("message") or "百度网盘创建文件失败"
                )
            return self._confirm_upload(save_path, upload_name, source)
        except Exception as error:
            logger.error(f"百度网盘本地文件上传失败：{source.name}，{error}")
            return False

    def _confirm_upload(
            self, save_path: str, upload_name: str, source: Path, retry: int = 10
    ) -> bool:
        self.files.refresh_directories()
        for index in range(max(1, retry)):
            if self.files.find_file(save_path, upload_name):
                logger.info(
                    f"百度网盘本地文件上传完成：{source.name} -> {save_path}/{upload_name}"
                )
                return True
            if index < retry - 1:
                time.sleep(1)
        logger.error(f"百度网盘上传完成后未找到目标文件：{save_path}/{upload_name}")
        return False
