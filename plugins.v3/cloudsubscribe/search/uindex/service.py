"""UIndex 搜索服务实现。"""

from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas.types import MediaType

from .client import UIndexClient, UIndexError
from ..magnet import clear_cache, media_titles, normalize_magnets
from ..matching import extract_season, extract_year, resource_title_matches, unique_texts
from ...core.search import SearchQuery


class UIndexSearchService:
    def __init__(self, client: UIndexClient, result_limit: int = 20):
        self._client = client
        self._result_limit = result_limit

    @classmethod
    def _is_mostly_ascii(cls, text: str) -> bool:
        """检查文本是否主要由 ASCII 字符构成（英文/外文标题）。"""
        if not text:
            return False
        stripped = "".join(ch for ch in text if ch.isalnum())
        if not stripped:
            return False
        ascii_count = sum(1 for ch in stripped if ord(ch) < 128)
        return (ascii_count / len(stripped)) >= 0.5

    @classmethod
    def _keywords(cls, mediainfo: Any, media_type: MediaType, season: Optional[int]) -> List[str]:
        raw_titles = media_titles(mediainfo)
        year = extract_year(getattr(mediainfo, "year", None))

        # 优先提取英文/原名标题（UIndex 为全外文BT索引库，优先使用原名）
        orig_title = str(
            getattr(mediainfo, "original_title", "")
            or getattr(mediainfo, "original_name", "")
            or getattr(mediainfo, "en_name", "")
            or ""
        ).strip()

        candidate_titles = []
        if orig_title and cls._is_mostly_ascii(orig_title):
            candidate_titles.append(orig_title)

        for t in raw_titles:
            if cls._is_mostly_ascii(t) and t not in candidate_titles:
                candidate_titles.append(t)

        # 若没有任何英文原名，兜底保留原始标题
        if not candidate_titles:
            candidate_titles = [t for t in raw_titles if t]

        keywords = []
        for t in candidate_titles:
            if media_type == MediaType.TV and season:
                keywords.append(f"{t} S{season:02d}")
            elif year:
                keywords.append(f"{t} {year}")
            keywords.append(t)
        return unique_texts(keywords)

    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        mediainfo = query.mediainfo
        if not mediainfo:
            return []

        keywords = self._keywords(mediainfo, query.media_type, query.season)
        if not keywords:
            return []

        limit = query.result_limit or self._result_limit
        exp_titles = media_titles(mediainfo)
        expected_year = extract_year(getattr(mediainfo, "year", None))
        collected = []
        seen_hashes = set()

        for kw in keywords[:3]:
            try:
                entries = self._client.search(kw)
            except UIndexError as err:
                logger.warning(f"UIndex 检索关键词 '{kw}' 失败：{err}")
                continue

            for item in entries:
                h = item.get("info_hash")
                if not h or h in seen_hashes:
                    continue

                # 季号匹配
                cand_season = extract_season(item.get("title"))
                if query.media_type == MediaType.TV and query.season:
                    if cand_season is not None and cand_season != query.season:
                        continue
                elif query.media_type == MediaType.MOVIE:
                    if cand_season is not None:
                        continue

                # 电影年份检查
                if query.media_type == MediaType.MOVIE:
                    cand_year = extract_year(item.get("title"))
                    if expected_year and cand_year and cand_year != expected_year:
                        continue

                # 标题校验：资源标题必须包含媒体目标标题中的至少一个
                if exp_titles and not resource_title_matches(
                        item.get("title"), exp_titles, expected_year=expected_year
                ):
                    continue
                seen_hashes.add(h)
                collected.append(item)
                if len(collected) >= limit:
                    break

            if len(collected) >= limit:
                break

        return normalize_magnets(collected, "uindex")

    def clear_cache(self) -> int:
        return clear_cache(self._client)
