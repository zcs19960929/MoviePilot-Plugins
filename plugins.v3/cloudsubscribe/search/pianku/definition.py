"""片库搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import PiankuClient
from .provider import PIANKU_RESOURCE_TYPES, create_pianku_provider
from .resource import PiankuResourceService
from .service import PiankuSearchService
from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition


class PiankuSourceDefinition(SearchSourceDefinition):
    """片库搜索渠道规范。"""

    id = "pianku"
    name = "片库"
    icon = "mdi-library-shelves"
    color = "deep-orange"
    order = 42

    #: 片库资源列表需要过盾与流式渲染，解析节奏由插件内部统一约束，不再暴露给用户。
    DETAIL_LIMIT = 3
    DETAIL_INTERVAL = 1.0
    GATE_TIMEOUT = 45

    @classmethod
    def configure_owner(cls, owner: Any, config: Dict[str, Any]) -> None:
        value = lambda key, default=None: cls.config_value(owner.__dict__, key, default)
        owner._pianku_base_url = str(
            value("pianku_base_url", "https://4k.pianku.online")
            or "https://4k.pianku.online"
        ).strip()
        owner._pianku_result_limit = max(
            1, min(int(value("pianku_result_limit", 20) or 20), 80)
        )
        owner._pianku_request_interval = max(
            0.5, min(float(value("pianku_request_interval", 1.5) or 1.5), 10.0)
        )
        owner._pianku_timeout = max(
            5, min(int(value("pianku_timeout", 120) or 120), 120)
        )

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        return PiankuClient(
            base_url=str(
                cls.config_value(config, "pianku_base_url", "https://4k.pianku.online")
                or "https://4k.pianku.online"
            ),
            proxy=ctx.get("proxy"),
            timeout=int(cls.config_value(config, "pianku_timeout", 120) or 120),
            request_interval=float(
                cls.config_value(config, "pianku_request_interval", 1.5) or 1.5
            ),
            detail_interval=cls.DETAIL_INTERVAL,
            gate_timeout=cls.GATE_TIMEOUT,
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="pianku",
                title="片库 (4k.pianku.online)",
                icon="mdi-library-shelves",
                fields=[
                    FieldSpec(
                        key="pianku_base_url",
                        label="站点地址",
                        placeholder="https://4k.pianku.online",
                        default="https://4k.pianku.online",
                        hint="默认 https://4k.pianku.online，可填入可用镜像域名。",
                        cols=12,
                    ),
                    FieldSpec(
                        key="test_pianku",
                        label="测试搜索",
                        type="test-source",
                        source="pianku",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="pianku",
                title="搜索与限速",
                icon="mdi-shield-search",
                hint="片库资源列表需人机验证，过盾与解析节奏由插件内部自动约束。",
                fields=[
                    FieldSpec(
                        key="pianku_result_limit",
                        label="候选上限",
                        type="number",
                        default=20,
                        min=1,
                        max=80,
                        cols=4,
                    ),
                    FieldSpec(
                        key="pianku_request_interval",
                        label="请求间隔",
                        type="number",
                        default=1.5,
                        min=0.5,
                        max=10,
                        step=0.1,
                        suffix="秒",
                        cols=4,
                    ),
                    FieldSpec(
                        key="pianku_timeout",
                        label="搜索超时",
                        type="number",
                        default=120,
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
        result_limit = int(cls.config_value(config, "pianku_result_limit", 20) or 20)
        detail_limit = cls.DETAIL_LIMIT
        resources = PiankuResourceService(client, result_limit, detail_limit)
        search_service = PiankuSearchService(
            client, resources, PIANKU_RESOURCE_TYPES, result_limit
        )
        return create_pianku_provider(
            search_service,
            PIANKU_RESOURCE_TYPES,
            {"result_limit": result_limit, "detail_limit": detail_limit},
        )
