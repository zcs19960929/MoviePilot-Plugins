"""UIndex 搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import UIndexClient
from .provider import create_uindex_provider
from .service import UIndexSearchService
from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition


class UIndexSourceDefinition(SearchSourceDefinition):
    """UIndex 搜索渠道规范。"""

    id = "uindex"
    name = "UIndex"
    icon = "mdi-magnet"
    color = "blue"
    order = 100

    @classmethod
    def configure_owner(cls, owner: Any, config: Dict[str, Any]) -> None:
        value = lambda key, default=None: cls.config_value(owner.__dict__, key, default)
        owner._uindex_base_url = str(
            value("uindex_base_url", "https://uindex.org") or "https://uindex.org"
        ).strip()
        owner._uindex_result_limit = max(
            1, min(int(value("uindex_result_limit", 20) or 20), 80)
        )
        owner._uindex_request_interval = max(
            0.2, min(float(value("uindex_request_interval", 1.0) or 1.0), 10.0)
        )
        owner._uindex_timeout = max(
            5, min(int(value("uindex_timeout", 60) or 60), 120)
        )

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        return UIndexClient(
            base_url=str(cls.config_value(config, "uindex_base_url", "https://uindex.org") or "https://uindex.org"),
            proxy=(context or {}).get("proxy"),
            request_timeout=int(cls.config_value(config, "uindex_timeout", 60) or 60),
            request_interval=float(cls.config_value(config, "uindex_request_interval", 1.0) or 1.0),
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="uindex",
                title="UIndex 磁力搜索",
                icon="mdi-magnet",
                fields=[
                    FieldSpec(
                        key="uindex_base_url",
                        label="服务地址",
                        placeholder="https://uindex.org",
                        default="https://uindex.org",
                        hint="默认 https://uindex.org，可填入自定义镜像。",
                        cols=12,
                    ),
                    FieldSpec(
                        key="test_uindex",
                        label="测试搜索",
                        type="test-source",
                        source="uindex",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="uindex",
                title="搜索与限速",
                icon="mdi-shield-search",
                hint="针对 UIndex 页面检索设置结果条数与并发间隔。",
                fields=[
                    FieldSpec(
                        key="uindex_result_limit",
                        label="候选上限",
                        type="number",
                        default=20,
                        min=1,
                        max=80,
                        cols=4,
                    ),
                    FieldSpec(
                        key="uindex_request_interval",
                        label="请求间隔",
                        type="number",
                        default=1.0,
                        min=0.2,
                        max=10,
                        step=0.1,
                        suffix="秒",
                        cols=4,
                    ),
                    FieldSpec(
                        key="uindex_timeout",
                        label="搜索超时",
                        type="number",
                        default=60,
                        min=5,
                        max=120,
                        suffix="秒",
                        cols=4,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        limit = int(cls.config_value(config, "uindex_result_limit", 20) or 20)
        return create_uindex_provider(
            UIndexSearchService(client, limit),
            {"result_limit": limit},
        )
