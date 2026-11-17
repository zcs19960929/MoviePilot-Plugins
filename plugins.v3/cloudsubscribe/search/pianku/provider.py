"""片库搜索能力声明。"""

from typing import Any, Iterable, Mapping

from ...core.search import (
    SearchCapability,
    SearchPolicy,
    SearchProvider,
)

#: 片库可稳定解析并交由后续转存链路的资源类型。
PIANKU_RESOURCE_TYPES = frozenset({
    "magnet",
    "ed2k",
    "quark",
    "baidu",
    "alipan",
    "uc",
    "xunlei",
    "guangya",
    "115",
    "123",
    "tianyi",
    "yun139",
})


def create_pianku_provider(
        service: Any,
        resource_types: Iterable[str],
        cache_context: Mapping[str, Any],
) -> SearchProvider:
    return SearchProvider(
        key="pianku",
        name="片库",
        resource_types=frozenset(resource_types),
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.RESOURCE_RESOLVE: service,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
        policy=SearchPolicy(cache_context=cache_context),
    )
