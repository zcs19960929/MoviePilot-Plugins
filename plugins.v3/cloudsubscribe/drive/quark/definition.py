"""夸克网盘自描述驱动规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import QuarkClient
from .provider import QuarkDrive, create_quark_provider
from ...core.definitions import (
    CheckinDefinition,
    DriverDefinition,
    FieldSpec,
    GroupSpec,
    build_checkin_definition,
)


class QuarkDriverDefinition(DriverDefinition):
    """夸克网盘驱动规范。"""

    id = "quark"
    name = "夸克网盘"
    icon = "mdi-cloud-outline"
    order = 30
    qrcode_service_type = QuarkClient
    qrcode_credentials = {"cookie": "quark_cookie"}
    qrcode_required_credentials = ("cookie",)
    qrcode_hint = "请使用夸克 App 扫描二维码"

    @classmethod
    def get_checkin_definition(cls) -> Optional[CheckinDefinition]:
        """自动注册夸克签到契约：启用开关由工厂生成，仅声明专属字段。"""
        return build_checkin_definition(
            cls,
            order=60,
            drive_key="quark",
            credential_attrs=("_quark_checkin_url",),
            credential_keys=("quark_checkin_url",),
            hint="请先配置并保存夸克签到 URL",
            group_title="夸克网盘签到",
            enable_label="启用每日签到并领取空间奖励",
            enable_cols=4,
            fields=[
                FieldSpec(
                    key="quark_checkin_url",
                    label="夸克签到 URL",
                    type="password",
                    default="",
                    hint="填写抓包得到的完整 growth/reward URL，需包含 kps、sign、vcode；参数失效后需重新抓取。",
                    cols=8,
                    show_condition="config.quark_checkin_enabled",
                ),
            ],
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="quark",
                title="夸克账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="quark_account_info",
                        type="account",
                        account_key="drive:quark",
                        cols=12,
                    ),
                    FieldSpec(
                        key="quark_cookie",
                        label="夸克 Cookie",
                        type="password",
                        hint="可直接填写或点击右侧二维码按钮扫码登录",
                        scan_provider="quark",
                        cols=12,
                    ),
                    FieldSpec(
                        key="quark_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="quark",
                        hint="夸克分享先保存到此路径，之后按平台规则整理。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="quark_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="quark",
                        hint="最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="quark",
                title="请求与风控",
                icon="mdi-timer-cog-outline",
                hint="夸克接口超时设置；分享转存风控使用“转存设置”中的公共配置。",
                fields=[
                    FieldSpec(
                        key="quark_request_timeout",
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
        cookie = str(config.get("_quark_cookie") or config.get("quark_cookie") or "").strip()
        if not cookie:
            return None
        persist = ctx.get("persist_config")
        client = QuarkClient(
            cookie=cookie,
            on_cookie_refresh=(
                lambda value: persist(quark_cookie=value)
                if callable(persist) else None
            ),
            timeout=int(config.get("_quark_request_timeout") or config.get("quark_request_timeout") or 30),
            checkin_url=str(
                config.get("_quark_checkin_url")
                or config.get("quark_checkin_url")
                or ""
            ),
        )
        client.risk_cooldown = int(
            config.get("_transfer_risk_cooldown")
            or config.get("transfer_risk_cooldown")
            or 1800
        )
        return client

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        return create_quark_provider(QuarkDrive(client=client))
