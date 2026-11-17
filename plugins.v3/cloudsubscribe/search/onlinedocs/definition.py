"""在线文档搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import OnlineDocsClient
from .provider import create_onlinedocs_provider
from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition


class OnlineDocsSourceDefinition(SearchSourceDefinition):
    """在线文档搜索渠道规范。"""

    id = "onlinedocs"
    name = "在线文档"
    icon = "mdi-file-document-outline"
    order = 110

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        return OnlineDocsClient(
            documents=cls.config_value(config, "onlinedocs", []) or [],
            proxy=(context or {}).get("proxy"),
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="onlinedocs",
                title="在线文档",
                icon="mdi-file-document-outline",
                fields=[
                    FieldSpec(
                        key="onlinedocs",
                        label="在线文档",
                        type="online-documents",
                        default=[{"url": "", "resource_types": []}],
                        hint="每个文档可分别选择一个或多个资源类型。",
                        cols=12,
                    ),
                    FieldSpec(
                        key="test_onlinedocs",
                        label="测试搜索",
                        type="test-source",
                        source="onlinedocs",
                        cols=12,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        ctx = context or {}
        resource_types = ctx.get("resource_types", ())
        if not client or not resource_types:
            return None
        return create_onlinedocs_provider(client, resource_types)
