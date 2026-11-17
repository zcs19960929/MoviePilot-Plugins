"""猫眼自动订阅能力组装。"""
from typing import Iterator

from .client import MaoyanClient
from .service import MaoyanService
from ...core.subscribe import MediaCandidate, SubscribeContext, SubscribeProvider
from ...core.subscribe.provider import ranking_scan_limit
from ...core.subscribe.registry import register

PLATFORMS = {
    "all": "", "tx": "3", "iqiyi": "2", "youku": "1",
    "letv": "4", "mgtv": "7", "pptv": "6", "sohu": "5",
}
SERIES_TYPES = {"series": "4", "tv": "0", "web": "1", "variety": "2"}


@register
class MaoyanSubscribeProvider(SubscribeProvider):
    provider_id = "maoyan"
    provider_name = "猫眼榜单"

    def __init__(self, client: MaoyanClient | None = None, service: MaoyanService | None = None):
        self.client = client or MaoyanClient()
        self.service = service or MaoyanService()

    def spec(self) -> dict:
        return {"id": self.provider_id, "name": self.provider_name, "default_cron": "0 9 * * *"}

    def has_listening(self, options: dict) -> bool:
        return bool(options.get("movie_box", True) or options.get("web_platform_map"))

    def fetch(self, options: dict, context: SubscribeContext) -> Iterator[MediaCandidate]:
        limit = max(1, min(int(options.get("limit") or 10), 100))
        scan_limit = ranking_scan_limit(options)
        proxy = context.proxy_for(options.get("proxy"))
        base_url = str(options.get("base_url") or MaoyanClient.BASE_URL).strip()
        client = self.client if base_url.rstrip("/") == self.client.base_url else MaoyanClient(base_url)
        seen: set[str] = set()
        if options.get("movie_box", True):
            for item in self.service.movie_box(client.get_json("/dashboard-ajax/movie", proxy), scan_limit):
                if item.unique_seed not in seen:
                    item.source_meta["rank_key"] = "movie_box"
                    seen.add(item.unique_seed)
                    yield item
        # 剧集/网播热度榜
        html = client.get_text("/web-heat", proxy=proxy)
        if html:
            for item in self.service.web_heat(html, scan_limit, "all"):
                if item.unique_seed not in seen:
                    item.source_meta["rank_key"] = "web_heat:all"
                    seen.add(item.unique_seed)
                    yield item

def create_maoyan_provider() -> MaoyanSubscribeProvider:
    return MaoyanSubscribeProvider()
