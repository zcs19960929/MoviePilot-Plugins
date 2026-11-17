"""HDHaven 搜索能力提供者声明。"""

from ...core.search import SearchCapability, SearchPolicy, SearchProvider
from .service import HDHavenSearchService


def create_hdhaven_provider(service: HDHavenSearchService) -> SearchProvider:
    """组装并注册 HDHaven 搜索与转存能力。"""
    client = service.get_client()
    return SearchProvider(
        key="hdhaven",
        name="HDHaven",
        resource_types=service.resource_types,
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.RESOURCE_PREVIEW: service,
            SearchCapability.RESOURCE_FILE_PREVIEW: service,
            SearchCapability.RESOURCE_UNLOCK: service,
            SearchCapability.ACCOUNT: client,
            SearchCapability.CHECKIN: client,
            SearchCapability.POINT_BUDGET: service.budget,
            SearchCapability.CACHE_MAINTENANCE: service,
            SearchCapability.LIFECYCLE: service,
        },
        policy=SearchPolicy(
            cacheable=True,
            cache_empty_results=False,
            cache_context=service.cache_context,
            max_concurrency=1,
        ),
    )
