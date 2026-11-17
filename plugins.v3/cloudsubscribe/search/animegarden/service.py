"""AnimeGarden 动漫搜索服务。"""

import json
from typing import Any, Dict, List, Optional

from app.log import logger

from .client import AnimeGardenClient, AnimeGardenClientError
from ..magnet import media_titles, normalize_magnets
from ..matching import unique_texts
from ..subs_filter import (
    build_anime_candidate,
    extract_fansub_from_title,
    filter_fansubs,
    release_matches,
)
from ...core.search import SearchQuery
from ...utils.cache import create_platform_ttl_cache


class AnimeGardenSearchService:
    """基于 AnimeGarden REST API 的动漫搜索实现。

    Anime Garden 是动漫花园（dmhy.org）第三方镜像站以及动画 BT 资源聚合站。
    支持关键词高级检索、字幕组选择、服务端排除以及公共动漫集数/合集规范化。
    """

    def __init__(
            self,
            client: AnimeGardenClient,
            result_limit: int = 80,
            config: Optional[Dict[str, Any]] = None,
    ):
        self._client = client
        self._result_limit = max(1, min(int(result_limit or 80), 200))
        self._config = config or {}
        self._search_cache = create_platform_ttl_cache(
            "animegarden_search",
            getattr(client, "base_url", ""),
            maxsize=256,
            ttl=int(self._config.get("search_cache_ttl_minutes") or 30) * 60,
        )

    def _get_api_filters(self) -> Dict[str, Any]:
        """获取适用于 AnimeGarden API 的高级筛选条件（服务端初筛排除）。"""
        # 从排除规则中提取简单关键词辅助服务端初筛
        exclude_re = str(self._config.get("animegarden_exclude_re") or "").strip()
        excludes = []
        if "720" in exclude_re:
            excludes.append("720p")
        if "特别篇" in exclude_re or "特別篇" in exclude_re:
            excludes.append("特别篇")

        return {"exclude": excludes}

    def _cached_search(self, keyword: str, is_list_mode: bool = False) -> List[Dict[str, Any]]:
        """带 TTL 缓存的关键词底层检索。"""
        filters = self._get_api_filters()
        cache_key = json.dumps({
            "keyword": keyword,
            "exclude": filters["exclude"],
            "list_mode": is_list_mode,
        })
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]

        if is_list_mode:
            all_rows = []
            for page in range(1, 6):
                rows = self._client.search(
                    keyword=keyword,
                    exclude=filters["exclude"],
                    page=page,
                    page_size=200,
                )
                if not rows:
                    break
                all_rows.extend(rows)
                if len(rows) < 200:
                    break
            self._search_cache[cache_key] = all_rows
            return all_rows

        rows = self._client.search(
            keyword=keyword,
            exclude=filters["exclude"],
            page=1,
            page_size=self._result_limit,
        )
        self._search_cache[cache_key] = rows
        return rows

    def search_keyword(self, keyword: str, season: Optional[int] = None) -> List[Dict[str, Any]]:
        """按纯关键词快速检索并转换为候选资源。"""
        rows = self._cached_search(keyword)
        season_num = season or 1
        results = []
        for row in rows:
            row_title = str(row.get("title") or "")
            magnet = str(row.get("magnet") or "").strip()
            if not magnet:
                continue
            fansub_name = (row.get("fansub") or {}).get("name") if isinstance(row.get("fansub"), dict) else None
            results.append(build_anime_candidate(
                title=row_title,
                magnet=magnet,
                size=int(row.get("size") or 0),
                source="animegarden",
                season=season_num,
                fansub=fansub_name,
            ))
        normalized = normalize_magnets(results, "animegarden")
        return filter_fansubs(normalized, config=self._config, prefix="animegarden", strict=False)[:self._result_limit]

    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        """按搜索请求在 AnimeGarden 检索磁力资源并解析集数与合集。"""
        is_list_mode = bool(getattr(query, "resource_list_mode", False))
        strict_filter = not is_list_mode

        explicit = str(getattr(query, "keyword", "") or "").strip()
        if explicit:
            titles = [explicit]
        else:
            base_titles = unique_texts(
                ([getattr(query.subscribe, "name", "")] if getattr(query, "subscribe", None) else [])
                + media_titles(getattr(query, "mediainfo", None))
                + ([getattr(query, "title", "")] if getattr(query, "title", None) else [])
                + ([getattr(query, "original_title", "")] if getattr(query, "original_title", None) else [])
            )
            expanded_titles = list(base_titles)
            for t in base_titles:
                for sep in ("，", "、", "：", ":", " - ", " ~ ", "～"):
                    if sep in t:
                        short = t.split(sep)[0].strip()
                        if short and len(short) >= 2 and short not in expanded_titles:
                            expanded_titles.append(short)
            titles = unique_texts(expanded_titles)
        if not titles:
            return []

        results = []
        season_num = query.season or 1
        for keyword in titles[:3]:
            try:
                rows = self._cached_search(keyword, is_list_mode=is_list_mode)
            except AnimeGardenClientError as e:
                logger.warning(f"[ANIMEGARDEN] 关键词={keyword} 请求异常：{e}")
                continue

            matched = [
                row for row in rows
                if release_matches(row.get("title", ""), titles, query.season, fuzzy=True)
            ]
            for row in matched:
                row_title = str(row.get("title") or "")
                magnet = str(row.get("magnet") or "").strip()
                if not magnet:
                    continue
                fansub_name = (row.get("fansub") or {}).get("name") if isinstance(row.get("fansub"), dict) else None
                if not fansub_name:
                    fansub_name = extract_fansub_from_title(row_title)
                results.append(build_anime_candidate(
                    title=row_title,
                    magnet=magnet,
                    size=int(row.get("size") or 0),
                    source="animegarden",
                    season=season_num,
                    fansub=fansub_name,
                ))
            logger.debug(
                f"[ANIMEGARDEN] 关键词={keyword}，返回={len(rows)}，匹配={len(matched)}"
            )

        normalized = normalize_magnets(results, "animegarden")
        before = len(normalized)
        normalized = filter_fansubs(
            normalized, config=self._config, prefix="animegarden", strict=strict_filter
        )
        if before != len(normalized):
            logger.debug(f"[ANIMEGARDEN] 字幕与排除过滤（strict={strict_filter}）：{before} -> {len(normalized)}")

        if is_list_mode:
            logger.info(f"[ANIMEGARDEN] 列表模式不限制数量，全量返回候选={len(normalized)}")
            return normalized

        limit = (
            min(self._result_limit, query.result_limit)
            if getattr(query, "result_limit", None)
            else self._result_limit
        )
        return normalized[:limit]

    def clear_cache(self) -> int:
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count
