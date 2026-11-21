"""按网盘目录重建 STRM 的维护接口。"""
from typing import Any, Dict

from .. import OwnerDelegator


class StrmMaintenanceApi(OwnerDelegator):
    """提供 STRM 扫描（预演）与重建（执行）接口。"""

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _run(self, payload: Dict[str, Any], dry_run: bool) -> dict:
        data = payload or {}
        service = getattr(self, "_strm_rebuild_service", None)
        if not service:
            return {
                "success": False,
                "message": "STRM 重建服务未初始化，请重启插件后重试",
            }
        return service.rebuild(
            str(data.get("cloud_path") or ""),
            recursive=bool(data.get("recursive", True)),
            dry_run=dry_run,
            overwrite=str(data.get("overwrite") or "stale"),
            limit=self._to_int(data.get("limit"), 5000),
        )

    def api_vue_strm_scan(self, payload: Dict[str, Any]) -> dict:
        """只统计不写盘，用于执行前预检。"""
        return self._run(payload, dry_run=True)

    def api_vue_strm_rebuild(self, payload: Dict[str, Any]) -> dict:
        """按目录实际生成/重写 STRM。"""
        return self._run(payload, dry_run=False)
