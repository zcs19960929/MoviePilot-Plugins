"""SeedHub 搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import SeedHubClient
from .provider import create_seedhub_provider
from .service import SeedHubSearchService
from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition


class SeedHubSourceDefinition(SearchSourceDefinition):
    """SeedHub 搜索渠道规范。"""

    id = "seedhub"
    name = "SeedHub"
    icon = "mdi-seed-outline"
    color = "deep-purple"
    order = 80

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        return SeedHubClient(
            base_url=str(
                cls.config_value(config, "seedhub_base_url", "https://www.seedhub.cc") or "https://www.seedhub.cc"),
            proxy=(context or {}).get("proxy"),
            request_timeout=int(cls.config_value(config, "seedhub_timeout", 60) or 60),
            request_interval=float(cls.config_value(config, "seedhub_request_interval", 0.3) or 0.3),
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="seedhub",
                title="SeedHub",
                icon="mdi-seed-outline",
                fields=[
                    FieldSpec(
                        key="seedhub_base_url",
                        label="服务地址",
                        placeholder="https://www.seedhub.cc",
                        default="https://www.seedhub.cc",
                        cols=12,
                    ),
                    FieldSpec(
                        key="test_seedhub",
                        label="测试搜索",
                        type="test-source",
                        source="seedhub",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="seedhub",
                title="搜索与风控",
                icon="mdi-shield-search",
                hint="搜索、详情和链接解析共享请求限速。",
                fields=[
                    FieldSpec(
                        key="seedhub_result_limit",
                        label="候选上限",
                        type="number",
                        default=20,
                        min=1,
                        max=80,
                        cols=4,
                    ),
                    FieldSpec(
                        key="seedhub_request_interval",
                        label="请求间隔",
                        type="number",
                        default=1.0,
                        min=1,
                        max=10,
                        step=0.1,
                        suffix="秒",
                        cols=4,
                    ),
                    FieldSpec(
                        key="seedhub_timeout",
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
        ctx = context or {}
        if not client:
            return None
        limit = int(cls.config_value(config, "seedhub_result_limit", 20) or 20)
        return create_seedhub_provider(
            SeedHubSearchService(client, limit),
            {"result_limit": limit},
        )
