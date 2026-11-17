"""百度网盘驱动配置。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import BaiduPanClient
from .provider import BaiduDrive, create_baidu_provider
from ...core.definitions import DriverDefinition, FieldSpec, GroupSpec


class BaiduDriverDefinition(DriverDefinition):
    """百度网盘驱动规范。"""

    id = "baidu"
    name = "百度网盘"
    icon = "mdi-cloud-circle-outline"
    order = 80
    qrcode_service_type = BaiduPanClient
    qrcode_credentials = {"cookie": "baidu_cookie"}
    qrcode_required_credentials = ("cookie",)
    qrcode_hint = "请使用百度网盘 App 扫码，并在手机上确认登录"

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="baidu",
                title="百度账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="baidu_account_info",
                        type="account",
                        account_key="drive:baidu",
                        cols=12,
                    ),
                    FieldSpec(
                        key="baidu_cookie",
                        label="百度网盘 Cookie",
                        type="password",
                        default="",
                        scan_provider="baidu",
                        hint="可点击右侧二维码按钮使用百度网盘 App 扫码登录，也可手动填写包含 BDUSS、STOKEN 的完整 Cookie。",
                        cols=12,
                    ),
                    FieldSpec(
                        key="baidu_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="baidu",
                        hint="百度分享和离线任务先保存到此路径，跨盘临时目录也创建在此路径下。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="baidu_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="baidu",
                        hint="最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="baidu",
                title="请求超时",
                icon="mdi-timer-cog-outline",
                hint="作用于百度网盘 HTTP 请求与分片上传，范围 5-300 秒。",
                fields=[
                    FieldSpec(
                        key="baidu_request_timeout",
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
        cookie = str(
            config.get("_baidu_cookie") or config.get("baidu_cookie") or ""
        ).strip()
        if not cookie:
            return None
        client = BaiduPanClient(
            cookie=cookie,
            timeout=int(
                config.get("_baidu_request_timeout")
                or config.get("baidu_request_timeout")
                or 30
            ),
        )
        client.risk_cooldown = max(60, int(
            config.get("_transfer_risk_cooldown")
            or config.get("transfer_risk_cooldown")
            or 1800
        ))
        return client

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        return create_baidu_provider(BaiduDrive(client=client)) if client else None
