"""115 网盘驱动自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import P115CheckinError, P115ClientManager
from .provider import create_p115_provider
from ...core.definitions import (
    CheckinDefinition,
    DriverDefinition,
    FieldSpec,
    GroupSpec,
    build_checkin_definition,
)


class P115DriverDefinition(DriverDefinition):
    """115 网盘驱动规范。"""

    id = "115"
    name = "115网盘"
    icon = "mdi-cloud-outline"
    order = 10
    qrcode_service_type = P115ClientManager
    qrcode_credentials = {"cookie": "cookies"}
    qrcode_required_credentials = ("cookie",)
    qrcode_hint = "请使用115客户端扫描二维码"
    qrcode_channels = (
        {"title": "网页", "value": "web"},
        {"title": "TV", "value": "tv"},
        {"title": "苹果", "value": "115ios"},
        {"title": "安卓", "value": "115android"},
        {"title": "iPad", "value": "115ipad"},
        {"title": "Windows", "value": "os_windows"},
        {"title": "MacOS", "value": "os_mac"},
        {"title": "Linux", "value": "os_linux"},
        {"title": "微信", "value": "wechatmini"},
        {"title": "支付宝", "value": "alipaymini"},
        {"title": "鸿蒙", "value": "harmony"},
    )

    @classmethod
    def get_checkin_definition(cls) -> Optional[CheckinDefinition]:
        """自动注册 115 签到契约：仅声明与驱动默认值不同的差异项。"""
        return build_checkin_definition(
            cls,
            key="p115",
            name="115 网盘",
            order=50,
            points_label="枫叶",
            drive_key="115",
            credential_attrs=("_cookies",),
            credential_keys=("cookies",),
            error_types=(P115CheckinError,),
            hint="请先配置并保存 115 Cookie",
            group_title="115 网盘签到",
            enable_label="启用每日签到并领取枫叶",
            enable_hint="复用已配置的 115 Cookie；每天先检查签到状态，未签到时自动领取枫叶。",
            enable_cols=12,
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="115",
                title="115账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="account_info",
                        type="account",
                        account_key="drive:115",
                        cols=12,
                    ),
                    FieldSpec(
                        key="cookies",
                        label="115 Cookie",
                        type="password",
                        default="",
                        hint="可直接填写或点击右侧二维码按钮扫码登录",
                        scan_provider="115",
                        cols=12,
                    ),
                    FieldSpec(
                        key="p115_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="115",
                        hint="115 分享和离线任务先保存到此路径，跨盘临时目录也创建在此路径下。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="p115_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="115",
                        hint="最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="115",
                title="请求超时",
                icon="mdi-timer-cog-outline",
                hint="仅作用于 115 网盘 API；0 表示该项不限制。",
                fields=[
                    FieldSpec(
                        key="timeout_enabled",
                        label="启用请求超时控制",
                        type="switch",
                        default=True,
                        cols=12,
                    ),
                    FieldSpec(
                        key="timeout_default_connect",
                        label="普通连接超时（秒）",
                        type="number",
                        default=30,
                        min=0,
                        cols=3,
                        show_condition="config.timeout_enabled",
                    ),
                    FieldSpec(
                        key="timeout_default_pool",
                        label="普通连接池超时（秒）",
                        type="number",
                        default=15,
                        min=0,
                        cols=3,
                        show_condition="config.timeout_enabled",
                    ),
                    FieldSpec(
                        key="timeout_default_read",
                        label="普通读取超时（秒）",
                        type="number",
                        default=60,
                        min=0,
                        cols=3,
                        show_condition="config.timeout_enabled",
                    ),
                    FieldSpec(
                        key="timeout_default_write",
                        label="普通写入超时（秒）",
                        type="number",
                        default=60,
                        min=0,
                        cols=3,
                        show_condition="config.timeout_enabled",
                    ),
                    FieldSpec(
                        key="timeout_slow_connect",
                        label="慢操作连接超时（秒）",
                        type="number",
                        default=30,
                        min=0,
                        cols=3,
                        show_condition="config.timeout_enabled",
                    ),
                    FieldSpec(
                        key="timeout_slow_pool",
                        label="慢操作连接池超时（秒）",
                        type="number",
                        default=15,
                        min=0,
                        cols=3,
                        show_condition="config.timeout_enabled",
                    ),
                    FieldSpec(
                        key="timeout_slow_read",
                        label="慢操作读取超时（秒）",
                        type="number",
                        default=300,
                        min=0,
                        cols=3,
                        show_condition="config.timeout_enabled",
                    ),
                    FieldSpec(
                        key="timeout_slow_write",
                        label="慢操作写入超时（秒）",
                        type="number",
                        default=300,
                        min=0,
                        cols=3,
                        show_condition="config.timeout_enabled",
                    ),
                ],
            ),
        ]

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        cookies = str(config.get("_cookies") or config.get("cookies") or "").strip()
        share_cache_ttl = int(config.get("_search_cache_ttl_minutes") or 30)
        return P115ClientManager(
            cookies=cookies,
            share_cache_ttl_minutes=share_cache_ttl,
            **dict(ctx.get("timeout_kwargs") or {}),
        )

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        return create_p115_provider(client)
