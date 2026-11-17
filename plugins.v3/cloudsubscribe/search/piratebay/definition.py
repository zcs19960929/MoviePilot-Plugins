"""海盗湾搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import PirateBayClient
from .provider import create_piratebay_provider
from .service import PirateBaySearchService
from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition


class PirateBaySourceDefinition(SearchSourceDefinition):
    """海盗湾搜索渠道规范。"""

    id = "piratebay"
    name = "海盗湾"
    icon = "mdi-pirate"
    color = "teal"
    order = 90

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        return PirateBayClient(
            base_url=str(cls.config_value(config, "piratebay_base_url", "https://apibay.org") or "https://apibay.org"),
            proxy=(context or {}).get("proxy"),
            timeout=int(cls.config_value(config, "piratebay_timeout", 60) or 60),
            request_interval=float(cls.config_value(config, "piratebay_request_interval", 1.0) or 1.0),
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="piratebay",
                title="海盗湾 (The Pirate Bay)",
                icon="mdi-pirate",
                fields=[
                    FieldSpec(
                        key="piratebay_base_url",
                        label="API 接口地址",
                        placeholder="https://apibay.org",
                        default="https://apibay.org",
                        hint="默认 https://apibay.org，可填入可用镜像。",
                        cols=12,
                    ),
                    FieldSpec(
                        key="test_piratebay",
                        label="测试搜索",
                        type="test-source",
                        source="piratebay",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="piratebay",
                title="搜索与限速",
                icon="mdi-shield-search",
                hint="针对海盗湾 API 设置结果条数与并发间隔。",
                fields=[
                    FieldSpec(
                        key="piratebay_result_limit",
                        label="候选上限",
                        type="number",
                        default=20,
                        min=1,
                        max=80,
                        cols=4,
                    ),
                    FieldSpec(
                        key="piratebay_request_interval",
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
                        key="piratebay_timeout",
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
        limit = int(cls.config_value(config, "piratebay_result_limit", 20) or 20)
        return create_piratebay_provider(
            PirateBaySearchService(client, limit),
            {"result_limit": limit},
        )
