"""123 网盘自描述驱动规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import P123ClientManager
from .provider import P123Drive, create_p123_provider
from ...core.definitions import DriverDefinition, FieldSpec, GroupSpec


class P123DriverDefinition(DriverDefinition):
    """123 网盘驱动自描述规范。"""

    id = "123"
    name = "123网盘"
    icon = "mdi-cloud-outline"
    order = 20
    qrcode_service_type = P123ClientManager
    qrcode_credentials = {"token": "p123_token"}
    qrcode_required_credentials = ("token",)
    qrcode_hint = "请使用 123 云盘 App 扫描二维码"

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="123",
                title="123账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="p123_account_info",
                        type="account",
                        account_key="drive:123",
                        cols=12,
                    ),
                    FieldSpec(
                        key="p123_token",
                        label="123 Token",
                        type="password",
                        hint="推荐点击右侧二维码按钮，使用 123 云盘 App 扫码登录",
                        scan_provider="123",
                        cols=12,
                    ),
                    FieldSpec(
                        key="p123_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="123",
                        hint="123分享和离线任务先保存到此路径，之后按平台规则整理。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="p123_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="123",
                        hint="最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="123",
                title="请求超时",
                icon="mdi-timer-cog-outline",
                hint="作用于 123 网盘登录和 HTTP 请求，范围 5-300 秒。",
                fields=[
                    FieldSpec(
                        key="p123_request_timeout",
                        label="请求超时（秒）",
                        type="number",
                        default=30,
                        min=5,
                        max=300,
                        cols=6,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        token = str(config.get("_p123_token") or config.get("p123_token") or "").strip()
        if not token:
            return None
        return P123ClientManager(
            token=token,
            timeout=float(config.get("_p123_request_timeout") or config.get("p123_request_timeout") or 30),
        )

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        return create_p123_provider(P123Drive(client=client))
