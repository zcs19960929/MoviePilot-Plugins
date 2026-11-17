"""小雅渠道自描述规范与配置表单。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import XiaoyaClient
from .provider import create_xiaoya_provider
from .resource import XiaoyaResourceService
from .service import XiaoyaSearchService
from ..types import RESOURCE_TYPE_ORDER, SUPPORTED_CLOUD_TYPES, SUPPORTED_RESOURCE_TYPES
from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition


class XiaoyaSourceDefinition(SearchSourceDefinition):
    """小雅渠道规范。"""

    id = "xiaoya"
    name = "小雅"
    icon = "mdi-folder-play-outline"
    color = "deep-purple"
    order = 50

    @classmethod
    def _resolve_data_dir(cls, context: Optional[Dict[str, Any]]) -> Path:
        """索引库存放路径。"""
        ctx = context or {}
        owner = ctx.get("storage_owner")
        if owner and hasattr(owner, "get_data_path"):
            try:
                p = owner.get_data_path()
                if p:
                    target = Path(p)
                    if (target / "xiaoya_index.db").exists():
                        return target
                    return target / "xiaoya"
            except Exception:
                pass
        return Path(tempfile.gettempdir()) / "cloudsubscribe_xiaoya"

    @classmethod
    def configure_owner(cls, owner: Any, config: Dict[str, Any]) -> None:
        value = lambda key, default=None: cls.config_value(owner.__dict__, key, default)
        owner._xiaoya_sync_url = str(
            value("xiaoya_sync_url", XiaoyaClient.DEFAULT_SYNC_URL) or XiaoyaClient.DEFAULT_SYNC_URL
        ).strip()
        owner._xiaoya_auto_sync_days = max(
            1, min(int(value("xiaoya_auto_sync_days", 1) or 1), 30)
        )
        owner._xiaoya_timeout = max(
            5, min(int(value("xiaoya_timeout", 60) or 60), 180)
        )
        owner._xiaoya_result_limit = max(
            1, min(int(value("xiaoya_result_limit", 20) or 20), 80)
        )

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        sync_url = cls.config_value(config, "xiaoya_sync_url", XiaoyaClient.DEFAULT_SYNC_URL)
        timeout = int(cls.config_value(config, "xiaoya_timeout", 60) or 60)

        return XiaoyaClient(
            sync_url=sync_url,
            proxy=ctx.get("proxy"),
            timeout=timeout,
        )

    @classmethod
    def create_resource_service(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> XiaoyaResourceService:
        ctx = context or {}
        data_dir = cls._resolve_data_dir(ctx)
        auto_sync_days = int(cls.config_value(config, "xiaoya_auto_sync_days", 1) or 1)
        return XiaoyaResourceService(
            client=client,
            data_dir=data_dir,
            auto_sync_days=auto_sync_days,
        )

    @classmethod
    def create_service(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        ctx = context or {}
        resources = cls.create_resource_service(client, config, context)
        # 资源服务绑定到客户端，供其实现账户卡片契约
        if hasattr(client, "bind_resources"):
            client.bind_resources(resources)

        active_resource_types = set(ctx.get("resource_types", ()) or SUPPORTED_RESOURCE_TYPES)
        # 仅保留当前系统启用的网盘，自动过滤不支持项
        effective_types = active_resource_types.intersection(set(SUPPORTED_CLOUD_TYPES))

        result_limit = int(cls.config_value(config, "xiaoya_result_limit", 20) or 20)

        return XiaoyaSearchService(
            resources=resources,
            resource_types=effective_types,
            result_limit=result_limit,
            drive_registry=ctx.get("drive_registry"),
        )

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not service or not client:
            return None
        ctx = context or {}
        resource_types = ctx.get("resource_types", ())
        return create_xiaoya_provider(
            service=service,
            client=client,
            resource_types=resource_types,
            cache_context={
                "sync_url": getattr(client, "sync_url", ""),
            },
        )

    @classmethod
    def build_test_context(
            cls, config: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """渠道测试按全部支持类型检索。"""
        return {"resource_type_order": list(RESOURCE_TYPE_ORDER)}

    @classmethod
    def get_account_info(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Dict[str, Any]:
        """收录统计，账户契约由客户端提供。"""
        if not client:
            return {"connected": False, "error": "小雅渠道未启用"}
        if hasattr(client, "get_account_info"):
            if not getattr(client, "_resources", None):
                resources = cls.create_resource_service(client, config, context)
                client.bind_resources(resources)
            return client.get_account_info(refresh=False)
        return {"connected": False, "error": "客户端未实现账户契约"}

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="xiaoya",
                title="连接配置",
                icon="mdi-server-network",
                fields=[
                    FieldSpec(
                        key="xiaoya_account_info",
                        type="account",
                        account_key="search:xiaoya",
                        compact=True,
                        cols=12,
                        # 卡片上的“清空重建索引”按钮及其确认文案
                        extra={"rebuild": "将清空本地索引库并重新全量下载构建，确定继续？"},
                    ),
                    FieldSpec(
                        key="xiaoya_sync_url",
                        label="服务地址",
                        placeholder="https://ailg.ggbond.org",
                        default="https://ailg.ggbond.org",
                        test_source="xiaoya",
                        hint="小雅索引数据源地址",
                        cols=12,
                    ),
                    FieldSpec(
                        key="test_xiaoya",
                        label="测试搜索",
                        type="test-source",
                        source="xiaoya",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="xiaoya",
                title="搜索与限速",
                icon="mdi-shield-search",
                hint="针对小雅索引的更新周期、下载超时与候选数量设置。",
                fields=[
                    FieldSpec(
                        key="xiaoya_result_limit",
                        label="候选上限",
                        type="number",
                        default=20,
                        min=1,
                        max=80,
                        suffix="条",
                        cols=4,
                    ),
                    FieldSpec(
                        key="xiaoya_auto_sync_days",
                        label="更新周期",
                        type="number",
                        default=1,
                        min=1,
                        max=30,
                        suffix="天",
                        cols=4,
                    ),
                    FieldSpec(
                        key="xiaoya_timeout",
                        label="下载超时",
                        type="number",
                        default=60,
                        min=5,
                        max=180,
                        suffix="秒",
                        cols=4,
                    ),
                ],
            ),
        ]
