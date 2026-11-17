"""天翼云盘自描述驱动规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import TianyiClient
from .provider import TianyiDrive, create_tianyi_provider
from ...core.definitions import DriverDefinition, FieldSpec, GroupSpec


class TianyiDriverDefinition(DriverDefinition):
    """天翼云盘驱动规范。"""

    id = "tianyi"
    name = "天翼云盘"
    icon = "mdi-cloud-outline"
    order = 50
    qrcode_service_type = TianyiClient
    qrcode_credentials = {
        "cookie": "tianyi_cookie",
        "access_token": "tianyi_access_token",
        "refresh_token": "tianyi_refresh_token",
        "session_key": "tianyi_session_key",
    }
    qrcode_required_credentials = ("session_key",)
    qrcode_hint = "请使用小翼管家、支付宝或天翼云盘 App 扫描二维码"

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="tianyi",
                title="天翼账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="tianyi_account_info",
                        type="account",
                        account_key="drive:tianyi",
                        cols=12,
                    ),
                    FieldSpec(
                        key="tianyi_cookie",
                        label="登录 Cookie",
                        type="password",
                        cols=12,
                        hint="可继续使用 Cookie 登录，也可点击右侧二维码扫码登录。",
                        scan_provider="tianyi",
                    ),
                    FieldSpec(
                        key="tianyi_access_token",
                        label="Access Token",
                        type="password",
                        cols=6,
                    ),
                    FieldSpec(
                        key="tianyi_refresh_token",
                        label="Refresh Token",
                        type="password",
                        cols=6,
                    ),
                    FieldSpec(
                        key="tianyi_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="tianyi",
                        hint="天翼跨盘上传和整理任务先保存到此路径，之后按规则整理。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="tianyi_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="tianyi",
                        hint="最终媒体从此目录开始按规则分类；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="tianyi",
                title="请求超时",
                icon="mdi-timer-cog-outline",
                hint="作用于天翼云盘 HTTP 请求，范围 10-300 秒。",
                fields=[
                    FieldSpec(
                        key="tianyi_request_timeout",
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
        cookie = str(config.get("_tianyi_cookie") or config.get("tianyi_cookie") or "").strip()
        access_token = str(config.get("_tianyi_access_token") or config.get("tianyi_access_token") or "").strip()
        refresh_token = str(config.get("_tianyi_refresh_token") or config.get("tianyi_refresh_token") or "").strip()
        session_key = str(config.get("_tianyi_session_key") or config.get("tianyi_session_key") or "").strip()
        if not cookie and not access_token and not refresh_token and not session_key:
            return None
        persist = ctx.get("persist_config")
        return TianyiClient(
            cookie=cookie,
            access_token=access_token,
            refresh_token=refresh_token,
            session_key=session_key,
            on_token_refresh=(
                lambda access, refresh, session: persist(
                    tianyi_access_token=access,
                    tianyi_refresh_token=refresh,
                    tianyi_session_key=session,
                ) if callable(persist) else None
            ),
            timeout=float(config.get("_tianyi_request_timeout") or config.get("tianyi_request_timeout") or 30),
        )

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        return create_tianyi_provider(TianyiDrive(client=client))
