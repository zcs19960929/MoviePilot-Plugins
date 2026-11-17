"""片库搜索服务：复用资源服务完成检索与按需解析。"""

from typing import Any, Iterable

from .client import PiankuClient
from .resource import PiankuResourceService
from ..magnet import clear_cache
from ...core.search import SearchQuery


class PiankuSearchService:
    """面向搜索注册表的能力门面，资源拉取与直链解析委托给资源服务。"""

    def __init__(
            self,
            client: PiankuClient,
            resources: PiankuResourceService,
            resource_types: Iterable[str],
            result_limit: int = 20,
    ) -> None:
        self._client = client
        self._resources = resources
        self._resource_types = tuple(resource_types)
        self._result_limit = max(1, int(result_limit or 20))

    def search(self, query: SearchQuery):
        return self._resources.normalize(self._resources.search(query))

    def resolve(self, **kwargs: Any):
        provider_data = kwargs.get("provider_data")
        detail_url = ""
        if isinstance(provider_data, dict):
            detail_url = str(provider_data.get("detail_url") or "")
        return self._resources.resolve_resource(
            reference=str(
                kwargs.get("resource_id")
                or kwargs.get("token")
                or kwargs.get("resource_ref")
                or ""
            ),
            resource_type=str(kwargs.get("resource_type") or ""),
            password=str(kwargs.get("password") or ""),
            title=str(kwargs.get("title") or kwargs.get("resource_title") or ""),
            detail_url=str(kwargs.get("detail_url") or detail_url),
        )

    def clear_cache(self) -> int:
        return clear_cache(self._resources)
