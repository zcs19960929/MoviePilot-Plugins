"""网盘提供方生命周期与凭据同步管理器。"""

from typing import Any, Dict, List, Optional

from app.log import logger

from .cloud import (
    CloudDriveCapability,
    CloudDriveProvider,
    CloudDriveRegistry,
)
from .transfer import CrossTransferTaskManager
from ..drive.scanner import DriverRegistry
from ..search.types import normalize_resource_type


class CloudDriveManager:
    """负责各类网盘客户端的组装、能力注册、Token刷新持久化与资源释放。"""

    def __init__(self, plugin: Any):
        self._plugin = plugin
        self.registry: CloudDriveRegistry = CloudDriveRegistry()
        self.active_drive: Optional[CloudDriveProvider] = None
        self._clients: Dict[str, Any] = {}
        self._qrcode_services: Dict[str, Any] = {}
        self.cross_transfer_manager: Optional[CrossTransferTaskManager] = None

    def initialize(self) -> None:
        """初始化全部网盘驱动与跨盘任务管理器。"""
        self.registry = CloudDriveRegistry()
        self._clients.clear()

        # 遍历所有已扫描的自包含驱动定义进行初始化
        for def_cls in DriverRegistry.get_definitions():
            self.register_provider(def_cls.id)

        self.cross_transfer_manager = CrossTransferTaskManager(
            self.registry.get,
            download_path=getattr(self._plugin, "_cross_transfer_download_path", ""),
            download_threads=getattr(self._plugin, "_cross_transfer_download_threads", 5),
            max_concurrent=getattr(self._plugin, "_cross_transfer_max_concurrent", 2),
            on_change=getattr(self._plugin, "_mark_runtime_changed", None),
        )

        cloud_key = getattr(self._plugin, "_cloud_drive_key", "115")
        try:
            self.active_drive = self.registry.get(cloud_key)
        except KeyError:
            logger.warning(f"网盘提供方 {cloud_key or '<empty>'} 未就绪，请检查对应账号配置")

        self._plugin._cloud_drive_resource_types = (
            frozenset(self.active_drive.resource_types)
            if self.active_drive else frozenset()
        )
        self._filter_supported_resource_types()

    def _persist_config(self, **updates: Any) -> None:
        """同步运行时凭据并写回插件完整配置。"""
        for key, value in updates.items():
            setattr(self._plugin, f"_{key}", value)
        persist = getattr(self._plugin, "_persist_config_values", None)
        if callable(persist):
            persist(**updates)

    def _definition_context(self, key: str) -> Dict[str, Any]:
        context: Dict[str, Any] = {"persist_config": self._persist_config}
        if key == "115":
            timeout_kwargs = getattr(self._plugin, "_p115_timeout_kwargs", None)
            context["timeout_kwargs"] = (
                timeout_kwargs() if callable(timeout_kwargs) else {}
            )
        return context

    def register_provider(self, key: str) -> bool:
        """根据网盘标识动态注册单个驱动。"""
        def_map = {d.id: d for d in DriverRegistry.get_definitions()}
        def_cls = def_map.get(key)
        if not def_cls:
            return False

        client = None
        try:
            client = def_cls.create_client(
                self._plugin.__dict__,
                self._definition_context(key),
            )
            if client:
                provider = def_cls.create_provider(client, self._plugin.__dict__)
                if provider:
                    old_client = self._clients.get(key)
                    self.registry.register(provider, replace=True)
                    self._clients[key] = client
                    qrcode_service = self._qrcode_services.pop(key, None)
                    if (
                            qrcode_service
                            and qrcode_service is not client
                            and not isinstance(qrcode_service, type)
                            and hasattr(qrcode_service, "close")
                    ):
                        qrcode_service.close()
                    if old_client and old_client is not client and hasattr(old_client, "close"):
                        try:
                            old_client.close()
                        except Exception as error:
                            logger.debug(f"关闭旧驱动 {key} 客户端失败：{error}")
                    if getattr(self._plugin, "_cloud_drive_key", "") == key:
                        self.active_drive = provider
                        self._plugin._cloud_drive = provider
                        self._plugin._cloud_drive_resource_types = frozenset(
                            provider.resource_types
                        )
                    return True
        except Exception as err:
            logger.warning(f"注册驱动 {key} 失败：{err}")
            if client and client is not self._clients.get(key) and hasattr(client, "close"):
                try:
                    client.close()
                except Exception:
                    pass

        return False

    def get_qrcode_service(self, key: str) -> Any:
        """返回已注册能力，未配置账号时按 Definition 创建临时扫码服务。"""
        normalized = str(key or "").strip().lower()
        try:
            provider = self.registry.get(normalized)
            if provider.supports(CloudDriveCapability.QRCODE_AUTH):
                return provider.require(CloudDriveCapability.QRCODE_AUTH)
        except KeyError:
            pass
        if normalized in self._qrcode_services:
            return self._qrcode_services[normalized]
        definition = next(
            (item for item in DriverRegistry.get_definitions() if item.id == normalized),
            None,
        )
        service = definition.create_qrcode_service() if definition else None
        if service is None:
            raise ValueError(f"网盘提供方不支持扫码登录：{normalized or '<empty>'}")
        self._qrcode_services[normalized] = service
        return service

    def get_client(self, key: str) -> Any:
        """获取指定网盘客户端。"""
        return self._clients.get(key)

    def _filter_supported_resource_types(self) -> None:
        """根据当前活动网盘的能力及跨盘配置，过滤不支持的资源类型。"""
        if not self.active_drive:
            return

        cross_enabled = getattr(self._plugin, "_cross_transfer_enabled", False)

        def resource_supported(value: str) -> bool:
            if self.active_drive.supports_resource_type(value):
                return True
            if not cross_enabled:
                return False
            try:
                source = self.registry.get(normalize_resource_type(value))
            except KeyError:
                return False
            return (
                    source.supports(CloudDriveCapability.SHARE_TRANSFER)
                    and source.supports(CloudDriveCapability.FILE_DOWNLOAD)
                    and self.active_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
            )

        resource_types: List[str] = list(getattr(self._plugin, "_resource_type_order", []) or [])
        supported = [val for val in resource_types if resource_supported(val)]
        unsupported = [val for val in resource_types if not resource_supported(val)]
        self._plugin._resource_type_order = supported

        if unsupported:
            logger.info(
                f"当前转存网盘为 {self.active_drive.name}，已忽略不支持的资源类型：{', '.join(unsupported)}"
            )

    def close(self) -> None:
        """安全释放所有网盘驱动资源。"""
        for key, client in list(self._clients.items()):
            if client and hasattr(client, "close"):
                try:
                    client.close()
                except Exception as error:
                    logger.debug(f"关闭驱动 {key} 客户端失败：{error}")

        self._clients.clear()
        for service in self._qrcode_services.values():
            if not isinstance(service, type) and hasattr(service, "close"):
                try:
                    service.close()
                except Exception:
                    pass
        self._qrcode_services.clear()
        self.active_drive = None
