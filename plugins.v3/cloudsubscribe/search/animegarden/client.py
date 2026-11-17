"""AnimeGarden REST API 搜索客户端。

Anime Garden 是动漫花园（dmhy.org）第三方镜像站以及动画 BT 资源聚合站。
文档规范见：https://animes.garden/docs/api
"""

from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from ...utils.http_client import (
    RequestGate,
    gated_request,
    normalize_proxies,
    request_error_summary,
    requests,
)


class AnimeGardenClientError(Exception):
    """AnimeGarden 客户端异常。"""
    pass


class AnimeGardenClient:
    """AnimeGarden API 客户端，使用共享门控 RequestGate 与高级检索能力。"""

    _HEADERS = {
        "User-Agent": "MoviePilot-CloudSubscribe-AnimeGarden/1.0",
        "Accept": "application/json",
    }

    def __init__(
            self,
            base_url: str = "https://api.animes.garden",
            timeout: int = 30,
            interval: float = 1.0,
            proxy: Optional[str] = None,
    ):
        raw_base = str(base_url or "https://api.animes.garden").strip().rstrip("/")
        # 若用户输入网站前台地址 https://animes.garden，自动转为其官方 API 服务 https://api.animes.garden
        if raw_base in {"https://animes.garden", "http://animes.garden"}:
            raw_base = "https://api.animes.garden"
        self.base_url = raw_base
        if urlparse(self.base_url).scheme not in {"http", "https"}:
            raise ValueError("AnimeGarden 服务地址必须为 HTTP(S)")
        self.timeout = max(5, min(int(timeout or 30), 60))
        self.interval = max(0.5, min(float(interval or 1.0), 30.0))
        self._proxy_address = proxy
        self.proxies = normalize_proxies(proxy)
        self._gate = RequestGate.shared(
            "AnimeGarden",
            f"{self.base_url}|{self._proxy_address}",
            request_interval=self.interval,
            minimum_interval=0.5,
            serial_requests=False,
        )

    def search(
            self,
            keyword: Optional[str] = None,
            include: Optional[Iterable[str]] = None,
            exclude: Optional[Iterable[str]] = None,
            page: int = 1,
            page_size: int = 80,
    ) -> List[Dict[str, Any]]:
        """向 AnimeGarden API 发起搜索，支持关键词、高级排除。"""
        query_params = [
            ("page", max(1, int(page or 1))),
            ("pageSize", max(1, min(int(page_size or 80), 200))),
        ]
        if keyword:
            query_params.append(("include", str(keyword).strip()))
        if include:
            for inc in include:
                s = str(inc or "").strip()
                if s and s != keyword:
                    query_params.append(("include", s))
        if exclude:
            for exc in exclude:
                s = str(exc or "").strip()
                if s:
                    query_params.append(("exclude", s))

        def _requester() -> Any:
            return requests.get(
                f"{self.base_url}/resources",
                params=query_params,
                timeout=self.timeout,
                proxies=self.proxies,
                headers=self._HEADERS,
            )

        try:
            response = gated_request(
                self._gate,
                _requester,
                retry_exceptions=(requests.exceptions.Timeout, requests.exceptions.ConnectionError),
                max_retries=2,
                initial_delay=0.8,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return []
            resources = data.get("resources")
            return resources if isinstance(resources, list) else []
        except Exception as exc:
            summary = request_error_summary(exc)
            raise AnimeGardenClientError(f"请求 AnimeGarden 服务器失败：{summary}") from exc
