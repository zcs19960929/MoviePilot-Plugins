"""小雅渠道能力声明。"""

from typing import Any, Iterable, Mapping

from ...core.search import (
    SearchCapability,
    SearchPolicy,
    SearchProvider,
)
from ..types import SUPPORTED_CLOUD_TYPES


def create_xiaoya_provider(
        service: Any,
        client: Any,
        resource_types: Iterable[str],
        cache_context: Mapping[str, Any],
) -> SearchProvider:
    """构建小雅 Provider。"""
    types = frozenset(resource_types) if resource_types else frozenset(SUPPORTED_CLOUD_TYPES)
    return SearchProvider(
        key="xiaoya",
        name="小雅",
        resource_types=types,
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.RESOURCE_FILE_PREVIEW: service,
            SearchCapability.ACCOUNT: client,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
        policy=SearchPolicy(
            cacheable=True,
            cache_context=cache_context,
        ),
    )
