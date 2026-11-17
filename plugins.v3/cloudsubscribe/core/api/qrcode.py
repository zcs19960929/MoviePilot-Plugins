"""网盘扫码登录 API。"""

from base64 import b64encode
import inspect
from io import BytesIO
from typing import Any, Dict

import qrcode.image.svg
from app.log import logger

import qrcode
from ..cloud import CloudDriveCapability
from ..delegation import OwnerDelegator
from ...drive.scanner import DriverRegistry


class QRCodeService(OwnerDelegator):
    """按 Provider 能力创建二维码并写回登录凭证。"""

    @staticmethod
    def _svg_qrcode(value: str) -> str:
        image = qrcode.make(
            value,
            image_factory=qrcode.image.svg.SvgPathImage,
            box_size=8,
            border=2,
        )
        output = BytesIO()
        image.save(output)
        return f"data:image/svg+xml;base64,{b64encode(output.getvalue()).decode('ascii')}"

    @staticmethod
    def _qrcode_definition(key: str):
        return next(
            (item for item in DriverRegistry.get_definitions() if item.id == key),
            None,
        )

    def _qrcode_service(self, provider: str):
        key = str(provider or "115").strip().lower()
        definition = self._qrcode_definition(key)
        if definition is None:
            raise ValueError(f"不支持扫码登录的网盘提供方：{key}")
        manager = getattr(self, "_drive_manager", None)
        if manager:
            return key, manager.get_qrcode_service(key), definition
        if not self._cloud_drive_registry:
            raise RuntimeError("网盘提供方尚未初始化")
        drive = self._cloud_drive_registry.get(key)
        return key, drive.require(CloudDriveCapability.QRCODE_AUTH), definition

    def api_vue_get_qrcode(
            self, provider: str = "115", client_type: str = "alipaymini"
    ) -> dict:
        try:
            key, service, definition = self._qrcode_service(provider)
            data = dict(service.create_qrcode_login(client_type) or {})
            qr_value = str(
                data.get("qr_url")
                or data.get("verification_uri_complete")
                or data.get("verification_uri")
                or ""
            ).strip()
            if not data.get("qrcode") and qr_value:
                data["qrcode"] = self._svg_qrcode(qr_value)
            if not data.get("qrcode"):
                raise RuntimeError("网盘接口未返回可用二维码")
            data["provider"] = key
            data["meta"] = definition.qrcode_meta()
            return {"success": True, "data": data}
        except Exception as error:
            logger.error(f"获取网盘登录二维码失败：{provider} - {error}")
            return {"success": False, "message": str(error)}

    def api_vue_check_qrcode(
            self,
            provider: str = "115",
            uid: str = "",
            time: str = "",
            sign: str = "",
            client_type: str = "alipaymini",
            qr_token: str = "",
            uni_id: str = "",
            device_code: str = "",
            device_id: str = "",
            client_id: str = "",
            t: str = "",
            ck: str = "",
            uuid: str = "",
            encryuuid: str = "",
            req_id: str = "",
            lt: str = "",
            param_id: str = "",
    ) -> dict:
        try:
            return self._check_qrcode_session(provider, {
                "uid": uid,
                "time": time,
                "sign": sign,
                "client_type": client_type,
                "qr_token": qr_token,
                "uni_id": uni_id,
                "device_code": device_code,
                "device_id": device_id,
                "client_id": client_id,
                "t": t,
                "ck": ck,
                "uuid": uuid,
                "encryuuid": encryuuid,
                "req_id": req_id,
                "lt": lt,
                "param_id": param_id,
            })
        except Exception as error:
            logger.error(f"检查网盘扫码登录状态失败：{provider} - {error}")
            return {"success": False, "message": str(error)}

    def api_vue_check_qrcode_post(self, payload: Dict[str, Any]) -> dict:
        """POST 版本的扫码状态检查，避免把临时凭据放进 URL。"""
        data = dict(payload or {})
        provider = str(data.pop("provider", "115") or "115")
        try:
            return self._check_qrcode_session(provider, data)
        except Exception as error:
            logger.error(f"检查网盘扫码登录状态失败：{provider} - {error}")
            return {"success": False, "message": str(error)}

    def _check_qrcode_session(
            self, provider: str, session: Dict[str, Any]
    ) -> dict:
        key, service, definition = self._qrcode_service(provider)
        params = {
            name: value for name, value in dict(session or {}).items()
            if name not in {"qrcode", "qr_url", "meta", "provider", "interval"}
               and value not in (None, "")
        }
        if "time" in params and "qrcode_time" not in params:
            params["qrcode_time"] = params.pop("time")
        checker = service.check_qrcode_login
        signature = inspect.signature(checker)
        if not any(
                item.kind == inspect.Parameter.VAR_KEYWORD
                for item in signature.parameters.values()
        ):
            params = {
                name: value for name, value in params.items()
                if name in signature.parameters
            }
        result = dict(checker(**params) or {})
        if result.get("status") != "success":
            return {"success": True, "provider": key, **result}

        credentials = definition.qrcode_config_values(result)
        for config_key, value in credentials.items():
            setattr(self, f"_{config_key}", value)
        self._persist_config_values(**credentials)
        manager = getattr(self, "_drive_manager", None)
        if manager:
            manager.register_provider(key)
        self._init_handlers()
        from .account import clear_account_cache
        from .page import clear_ui_options_cache
        clear_account_cache(f"drive:{key}")
        clear_ui_options_cache()
        logger.info(f"{key} 扫码登录成功")
        return {
            "success": True,
            "provider": key,
            "status": "success",
            "message": result.get("message") or "登录成功",
            "credentials": credentials,
        }
