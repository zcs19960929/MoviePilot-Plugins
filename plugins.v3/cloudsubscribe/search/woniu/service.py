"""蜗牛搜索服务。"""

from typing import Iterable

from app.schemas.types import MediaType

from .client import WoniuClient
from .resource import WoniuResourceService
from ..magnet import clear_cache, media_titles, normalize_magnets
from ...core.search import SearchQuery


class WoniuSearchService:
    """蜗牛搜索门面服务，对接系统统一检索编排。"""

    def __init__(
            self,
            client: WoniuClient,
            resources: WoniuResourceService,
            resource_types: Iterable[str],
            result_limit: int,
    ):
        self._client = client
        self._resources = resources
        self._resource_types = tuple(resource_types)
        self._result_limit = result_limit

    def search(self, query: SearchQuery):
        mediainfo = query.mediainfo
        titles = media_titles(mediainfo)
        resources = self._resources.search(
            title=titles[0] if titles else "",
            alternative_titles=titles[1:],
            year=getattr(mediainfo, "year", None),
            media_type=(
                "tv" if query.media_type == MediaType.TV else "movie"
            ),
            season=query.season,
            resource_type_order=self._resource_types,
            limit=(
                query.result_limit or self._result_limit
                if query.resource_list_mode else self._result_limit
            ),
            resource_list_mode=query.resource_list_mode,
        )
        return normalize_magnets(resources, "woniu")

    def resolve(self, **kwargs):
        return self._resources.resolve_resource(
            str(kwargs.get("resource_id") or "")
        )

    def clear_cache(self) -> int:
        return clear_cache(self._resources)
