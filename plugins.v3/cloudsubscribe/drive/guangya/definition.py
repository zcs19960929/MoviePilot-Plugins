"""光鸭网盘自描述驱动规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import GuangyaClient
from .provider import GuangyaDrive, create_guangya_provider
from ...core.definitions import DriverDefinition, FieldSpec, GroupSpec


class GuangyaDriverDefinition(DriverDefinition):
    """光鸭网盘驱动规范。"""

    id = "guangya"
    name = "光鸭网盘"
    icon = "mdi-cloud-outline"
    order = 40
    qrcode_service_type = GuangyaClient
    qrcode_credentials = {
        "access_token": "guangya_access_token",
        "refresh_token": "guangya_refresh_token",
        "client_id": "guangya_client_id",
        "device_id": "guangya_device_id",
    }
    qrcode_required_credentials = ("access_token",)
    qrcode_hint = "请使用光鸭网盘完成扫码授权"

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="guangya",
                title="光鸭账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="guangya_account_info",
                        type="account",
                        account_key="drive:guangya",
                        cols=12,
                    ),
                    FieldSpec(
                        key="guangya_access_token",
                        label="Access Token",
                        type="password",
                        hint="推荐点击右侧二维码按钮扫码登录",
                        scan_provider="guangya",
                        cols=6,
                    ),
                    FieldSpec(
                        key="guangya_refresh_token",
                        label="Refresh Token",
                        type="password",
                        cols=6,
                    ),
                    FieldSpec(
                        key="guangya_client_id",
                        label="Client ID",
                        placeholder="留空使用默认客户端",
                        cols=6,
                    ),
                    FieldSpec(
                        key="guangya_device_id",
                        label="Device ID",
                        placeholder="扫码登录后自动写入",
                        cols=6,
                    ),
                    FieldSpec(
                        key="guangya_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="guangya",
                        hint="光鸭分享和离线任务先保存到此路径，之后按平台规则整理。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="guangya_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="guangya",
                        hint="最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="guangya",
                title="请求超时",
                icon="mdi-timer-cog-outline",
                hint="作用于光鸭网盘 HTTP 请求，范围 5-300 秒。",
                fields=[
                    FieldSpec(
                        key="guangya_request_timeout",
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
        ctx = context or {}
        access_token = str(config.get("_guangya_access_token") or config.get("guangya_access_token") or "").strip()
        refresh_token = str(config.get("_guangya_refresh_token") or config.get("guangya_refresh_token") or "").strip()
        if not access_token and not refresh_token:
            return None
        persist = ctx.get("persist_config")
        return GuangyaClient(
            access_token=access_token,
            refresh_token=refresh_token,
            client_id=str(config.get("_guangya_client_id") or config.get("guangya_client_id") or "").strip() or None,
            device_id=str(config.get("_guangya_device_id") or config.get("guangya_device_id") or "").strip() or None,
            on_token_refresh=(
                lambda access, refresh: persist(
                    guangya_access_token=access,
                    guangya_refresh_token=refresh,
                ) if callable(persist) else None
            ),
            timeout=float(config.get("_guangya_request_timeout") or config.get("guangya_request_timeout") or 30),
        )

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        return create_guangya_provider(GuangyaDrive(client=client))
