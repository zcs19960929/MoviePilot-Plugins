"""阿里云盘自描述驱动规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import AliPanClient
from .provider import AliPanDrive, create_alipan_provider
from ...core.definitions import DriverDefinition, FieldSpec, GroupSpec


class AliPanDriverDefinition(DriverDefinition):
    """阿里云盘驱动规范。"""

    id = "alipan"
    name = "阿里云盘"
    icon = "mdi-cloud-outline"
    order = 60
    qrcode_service_type = AliPanClient
    qrcode_credentials = {
        "access_token": "alipan_access_token",
        "refresh_token": "alipan_refresh_token",
    }
    qrcode_required_credentials = ("refresh_token",)
    qrcode_hint = "请使用阿里云盘 App 扫描二维码"

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="alipan",
                title="阿里云盘账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="alipan_account_info",
                        type="account",
                        account_key="drive:alipan",
                        cols=12,
                    ),
                    FieldSpec(
                        key="alipan_access_token",
                        label="Access Token",
                        type="password",
                        cols=6,
                        hint="可直接填写现有 Access Token；扫码登录后会自动写回。",
                        scan_provider="alipan",
                    ),
                    FieldSpec(
                        key="alipan_refresh_token",
                        label="Refresh Token",
                        type="password",
                        cols=6,
                        hint="用于 Access Token 失效后自动刷新；扫码登录后会自动写回。",
                    ),
                    FieldSpec(
                        key="alipan_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="alipan",
                        hint="分享转存和跨盘上传先保存到此路径。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="alipan_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="alipan",
                        hint="最终媒体目录；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="alipan",
                title="请求超时",
                icon="mdi-timer-cog-outline",
                hint="作用于阿里云盘请求和分片上传，范围 10-300 秒。",
                fields=[
                    FieldSpec(
                        key="alipan_request_timeout",
                        label="请求超时（秒）",
                        type="number",
                        default=60,
                        min=10,
                        max=300,
                        cols=6,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        access_token = str(config.get("_alipan_access_token") or config.get("alipan_access_token") or "").strip()
        refresh_token = str(config.get("_alipan_refresh_token") or config.get("alipan_refresh_token") or "").strip()
        if not access_token and not refresh_token:
            return None
        persist = ctx.get("persist_config")
        return AliPanClient(
            access_token=access_token,
            refresh_token=refresh_token,
            on_token_refresh=(
                lambda access, refresh: persist(
                    alipan_access_token=access,
                    alipan_refresh_token=refresh,
                ) if callable(persist) else None
            ),
            timeout=float(config.get("_alipan_request_timeout") or config.get("alipan_request_timeout") or 30),
        )

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        return create_alipan_provider(AliPanDrive(client=client))
