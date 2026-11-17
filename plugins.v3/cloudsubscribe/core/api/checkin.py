"""通用签到 API。"""

from typing import Any, Dict, Optional

from .. import OwnerDelegator


class CheckinApi(OwnerDelegator):
    def api_vue_checkin_overview(self, days: int = 7) -> dict:
        return {
            "success": True,
            "data": self.get_checkin_overview(days=days),
        }

    def api_vue_checkin(
            self,
            provider: str,
            payload: Optional[Dict[str, Any]] = None,
    ) -> dict:
        request = payload or {}
        mode = str(request.get("mode") or "").strip().lower()
        return self.start_manual_checkin(
            provider=provider,
            mode=mode,
        )

    def api_vue_checkin_history(
            self,
            provider: str,
            limit: int = 20,
    ) -> dict:
        history = self.get_checkin_history(provider=provider, limit=limit)
        if history is None:
            return {"success": False, "message": "不支持的签到提供方"}
        return {"success": True, "data": history}

    def api_vue_checkin_histories(self, limit: int = 60) -> dict:
        """一次返回全部渠道的签到记录，供时间线首屏使用（单次快照读取）。"""
        return self.list_checkin_details(provider="", limit=limit)
