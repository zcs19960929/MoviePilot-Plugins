"""蜗牛资源搜索、过滤与直链解析服务。"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

from app.log import logger

from .client import WoniuClient, WoniuError
from ..magnet import parse_size_str
from ..matching import (
    extract_season,
    extract_year,
    title_matches,
)
from ..types import (
    SUPPORTED_RESOURCE_TYPES,
    append_share_password,
    resource_type_from_text,
    resource_type_from_url,
)
from ...utils.cache import create_platform_ttl_cache


class WoniuResourceService:
    """蜗牛资源处理服务，负责影片匹配、网盘链接解析与缓存。"""

    def __init__(self, client: WoniuClient):
        self._client = client
        self._lock = threading.RLock()
        cache_identity = (
            f"{getattr(client, 'base_url', '')}|"
            f"{str(getattr(client, 'username', '') or '').strip().casefold()}"
        )
        self._search_cache = create_platform_ttl_cache(
            "woniu:search", cache_identity, maxsize=256, ttl=15 * 60
        )
        self._resource_cache = create_platform_ttl_cache(
            "woniu:resources", cache_identity, maxsize=1024, ttl=15 * 60
        )

    @staticmethod
    def _extract_password(url: str, text: str) -> str:
        """从 URL 查询参数或附带描述文本中提取网盘分享密码。"""
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            for k in ("password", "pwd", "code"):
                if qs.get(k):
                    return str(qs[k][0]).strip()
        except Exception:
            pass

        match = re.search(r"(?:提取码|密码|访问码|code)\s*[:：]?\s*([A-Za-z0-9]{4,})", text, re.I)
        if match:
            return match.group(1).strip()
        return ""

    @staticmethod
    def _resource_type(url: str, title: str) -> str:
        """识别网盘类型（115、guangya、quark、123、alipan 等）。"""
        rtype = resource_type_from_url(url)
        if rtype:
            return rtype
        return resource_type_from_text(title)

    def _match_vod_card(
            self,
            card: Dict[str, Any],
            expected_titles: List[str],
            expected_year: str,
            media_type: str,
            expected_season: Optional[int],
    ) -> bool:
        title = card.get("title") or ""
        if not title_matches(title, expected_titles):
            return False

        meta = str(card.get("meta") or "")
        card_year = extract_year(meta) or extract_year(title)
        if expected_year and card_year:
            # 电影要求年份吻合；剧集容许 1 年误差
            if media_type == "movie" and card_year != expected_year:
                return False
            if media_type == "tv" and abs(int(card_year) - int(expected_year)) > 1:
                return False

        if media_type == "tv" and expected_season is not None:
            card_season = extract_season(title)
            if card_season is not None and card_season != expected_season:
                return False

        return True

    def search(
            self,
            title: str,
            alternative_titles: Optional[Iterable[str]] = None,
            year: Optional[str] = None,
            media_type: str = "movie",
            season: Optional[int] = None,
            resource_type_order: Optional[Iterable[str]] = None,
            limit: int = 10,
            resource_list_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        """执行影视检索、详情解析与资源过滤。"""
        if getattr(self._client, "is_banned", False):
            logger.warning("🐌 蜗牛账号已被封禁，已跳过搜索")
            raise WoniuError("蜗牛账号已被封禁", "woniu_account_banned")
        all_titles = list(dict.fromkeys(
            str(t).strip() for t in [title, *(alternative_titles or [])]
            if str(t or "").strip()
        ))
        if not all_titles:
            return []

        expected_year = str(year or "").strip()
        allowed_types = tuple(dict.fromkeys(
            str(rt).strip().casefold()
            for rt in (resource_type_order or ())
            if str(rt).strip().casefold() in SUPPORTED_RESOURCE_TYPES
        )) or tuple(SUPPORTED_RESOURCE_TYPES)

        order_map = {rt: idx for idx, rt in enumerate(allowed_types)}
        matched_cards: List[Dict[str, Any]] = []
        seen_vod_ids = set()

        # 优先使用主标题检索，未命中再逐一使用别名补充
        for kw in all_titles:
            cache_key = f"kw:{kw.casefold()}"
            cards = self._search_cache.get(cache_key)
            if cards is None:
                try:
                    cards = self._client.search_vods(kw)
                    self._search_cache[cache_key] = cards
                except WoniuError as error:
                    # 频控/封禁需冒泡给搜索编排做熔断，否则会被静默吞掉成“0 条候选”
                    if error.code in {"woniu_rate_limited", "woniu_account_banned"}:
                        raise
                    logger.debug(f"蜗牛搜索关键词 '{kw}' 失败：{error}")
                    cards = []
                except Exception as error:
                    logger.debug(f"蜗牛搜索关键词 '{kw}' 失败：{error}")
                    cards = []

            for card in cards:
                vid = str(card.get("id") or "").strip()
                if not vid or vid in seen_vod_ids:
                    continue
                if self._match_vod_card(card, all_titles, expected_year, media_type, season):
                    seen_vod_ids.add(vid)
                    matched_cards.append(card)

            if len(matched_cards) >= 3:
                break

        # 针对命中的影片提取详情直链资源
        collected_resources: List[Dict[str, Any]] = []

        for card in matched_cards:
            vid = card["id"]
            cache_key = f"detail:{vid}"
            items = self._resource_cache.get(cache_key)
            if items is None:
                try:
                    items = self._client.get_vod_detail(vid)
                    self._resource_cache[cache_key] = items
                except Exception as error:
                    logger.debug(f"蜗牛获取影片 #{vid} 详情失败：{error}")
                    items = []

            for idx, item in enumerate(items):
                raw_url = str(item.get("url") or "").strip()
                if not raw_url:
                    continue

                rtype = self._resource_type(raw_url, item.get("title", ""))
                if not rtype or (allowed_types and rtype not in order_map):
                    continue

                pwd = self._extract_password(raw_url, f"{item.get('title', '')} {item.get('raw_meta', '')}")
                final_url = append_share_password(rtype, raw_url, pwd) if pwd else raw_url

                res_title = str(item.get("title") or card.get("title") or "").strip()
                size_bytes = parse_size_str(res_title) or parse_size_str(item.get("raw_meta", ""))

                resource_id = f"woniu:{vid}:{idx}"
                resource_record = {
                    "resource_id": resource_id,
                    "title": res_title,
                    "url": final_url,
                    "resource_type": rtype,
                    "size": size_bytes,
                    "source": "woniu",
                    "identity_verified": True,
                    "target_season": season,
                    "description": card.get("meta") or "",
                }
                collected_resources.append(resource_record)

        # 按用户偏好的网盘渠道优先级排序
        collected_resources.sort(key=lambda r: order_map.get(r["resource_type"], 999))
        return collected_resources[:max(1, limit)]

    def resolve_resource(self, resource_id: str) -> Dict[str, Any]:
        """根据 resource_id 解析资源直链（直链已于详情页抓取阶段解析完毕）。"""
        for item in self._resource_cache.values():
            if isinstance(item, list):
                for entry in item:
                    if entry.get("id") == resource_id or f"woniu:{entry.get('id')}" == resource_id:
                        raw_url = entry.get("url")
                        rtype = self._resource_type(raw_url, entry.get("title", ""))
                        return {"url": raw_url, "resource_type": rtype}
        raise WoniuError(f"未找到指定的蜗牛资源：{resource_id}", "woniu_resource_not_found")

    def clear_cache(self) -> Dict[str, int]:
        count = len(self._search_cache) + len(self._resource_cache)
        self._search_cache.clear()
        self._resource_cache.clear()
        return {"resources": count}
