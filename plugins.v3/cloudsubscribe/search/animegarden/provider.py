"""AnimeGarden 搜索能力提供者定义。"""

from typing import Any, Mapping

from ...core.search import (
    SearchCapability,
    SearchPolicy,
    SearchProvider,
)


def create_animegarden_provider(
        service: Any,
        cache_context: Mapping[str, Any],
) -> SearchProvider:
    """创建 AnimeGarden 搜索能力提供者。"""
    return SearchProvider(
        key="animegarden",
        name="AnimeGarden",
        resource_types=frozenset({"magnet"}),
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
        policy=SearchPolicy(cache_context=cache_context),
    )
