"""移动云盘自描述驱动规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import Yun139Client
from .provider import Yun139Drive, create_yun139_provider
from ...core.definitions import DriverDefinition, FieldSpec, GroupSpec


class Yun139DriverDefinition(DriverDefinition):
    """移动云盘驱动规范。"""

    id = "yun139"
    name = "移动云盘"
    icon = "mdi-cloud-outline"
    order = 55
    qrcode_service_type = Yun139Client
    qrcode_credentials = {"authorization": "yun139_authorization"}
    qrcode_required_credentials = ("authorization",)
    qrcode_hint = "请使用中国移动云盘 App 扫描二维码"

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="yun139",
                title="移动云盘账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="yun139_account_info",
                        type="account",
                        account_key="drive:yun139",
                        cols=12,
                    ),
                    FieldSpec(
                        key="yun139_authorization",
                        label="Authorization 令牌",
                        type="password",
                        hint="从移动云盘网页请求头复制完整 Authorization 值，也可点击右侧二维码扫码登录。",
                        scan_provider="yun139",
                        cols=12,
                    ),
                    FieldSpec(
                        key="yun139_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="yun139",
                        hint="移动云盘整理任务使用的目标路径，默认根目录。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="yun139_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="yun139",
                        hint="最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="yun139",
                title="请求超时",
                icon="mdi-timer-cog-outline",
                hint="作用于移动云盘 HTTP 请求，范围 10-300 秒。",
                fields=[
                    FieldSpec(
                        key="yun139_request_timeout",
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
        authorization = str(
            config.get("_yun139_authorization")
            or config.get("yun139_authorization")
            or ""
        ).strip()
        if not authorization:
            return None
        persist = ctx.get("persist_config")
        return Yun139Client(
            authorization=authorization,
            on_token_refresh=(
                lambda value: persist(yun139_authorization=value)
                if callable(persist) else None
            ),
            timeout=float(
                config.get("_yun139_request_timeout")
                or config.get("yun139_request_timeout")
                or 60
            ),
        )

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        return create_yun139_provider(Yun139Drive(client=client))
