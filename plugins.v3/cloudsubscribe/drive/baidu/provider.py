"""百度网盘能力注册。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet

from .client import BaiduPanClient
from .files import BaiduFileService
from .offline import BaiduOfflineService
from .share import BaiduShareService
from .upload import BaiduUploadService
from ...core.cloud import (
    CloudDriveCapability,
    CloudDrivePolicy,
    CloudDriveProvider,
    CloudFile,
)


@dataclass
class BaiduDrive:
    client: BaiduPanClient
    page_size: int = 1000

    def close(self) -> None:
        self.client.close()
        BaiduPanClient.close_qrcode_sessions()


@dataclass(frozen=True)
class BaiduPlaybackReference:
    """按路径取链的播放引用。"""

    template_variables: FrozenSet[str] = frozenset({"fs_id", "path", "md5", "size"})
    proxy_identifier: str = ""

    @staticmethod
    def reference_values(file_item: CloudFile) -> Dict[str, str]:
        values = dict(file_item.playback_values or {})
        values.setdefault("path", str(file_item.id or ""))
        values.setdefault("md5", str(file_item.md5 or ""))
        values.setdefault("size", str(int(file_item.size or 0)))
        return {key: value for key, value in values.items() if str(value or "")}


def create_baidu_provider(drive: BaiduDrive) -> CloudDriveProvider:
    files = BaiduFileService(drive.client, drive.page_size)
    upload = BaiduUploadService(drive.client, files)
    share = BaiduShareService(drive.client, files)
    offline = BaiduOfflineService(drive.client, files)

    return CloudDriveProvider(
        key="baidu",
        name="百度网盘",
        config_prefix="baidu",
        resource_types=frozenset({"baidu", "ed2k", "magnet"}),
        services={
            CloudDriveCapability.AUTHENTICATION: drive.client,
            CloudDriveCapability.ACCOUNT: drive.client,
            CloudDriveCapability.SHARE_TRANSFER: share,
            CloudDriveCapability.OFFLINE_DOWNLOAD: offline,
            CloudDriveCapability.DIRECTORY_READ: files,
            CloudDriveCapability.FILE_QUERY: files,
            CloudDriveCapability.FILE_MUTATION: files,
            CloudDriveCapability.BATCH_FILE_MUTATION: files,
            CloudDriveCapability.PLAYBACK_REFERENCE: BaiduPlaybackReference(),
            CloudDriveCapability.OFFLINE_TASKS: offline,
            CloudDriveCapability.LOCAL_UPLOAD: upload,
            CloudDriveCapability.FILE_DOWNLOAD: files,
            CloudDriveCapability.QRCODE_AUTH: BaiduPanClient,
        },
        policy=CloudDrivePolicy(
            pagination_mode="offset",
            max_page_size=1000,
            supports_batch=True,
            max_batch_size=100,
            supports_cancel=False,
            max_concurrency=2,
            cache_ttl_seconds={"path": 3600, "share_status": 600},
        ),
    )
