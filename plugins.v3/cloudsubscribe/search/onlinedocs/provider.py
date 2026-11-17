"""在线文档搜索能力声明。"""

from typing import Any, Iterable

from .service import OnlineDocumentSearchService
from ...core.search import (
    SearchCapability,
    SearchProvider,
)


def create_onlinedocs_provider(
        client: Any,
        resource_types: Iterable[str],
) -> SearchProvider:
    service = OnlineDocumentSearchService(client)
    return SearchProvider(
        key="onlinedocs",
        name="在线文档",
        resource_types=frozenset(resource_types),
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
    )
