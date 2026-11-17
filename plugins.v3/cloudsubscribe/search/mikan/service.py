"""蜜柑计划（Mikan）搜索服务层、番剧过滤与快速订阅匹配逻辑。"""

import time
from threading import RLock
from typing import Any, Dict, List, Optional

from app.log import logger

from .client import MikanClient, MikanClientError
from ..magnet import media_titles, normalize_magnets
from ..matching import (
    extract_mikan_rss_params,
    media_aliases,
    normalize_season,
    search_keyword_candidates,
    unique_texts,
)
from ..subs_filter import (
    filter_fansubs,
    is_pack_release,
    release_episodes,
    release_matches,
    release_season_matches,
)
from ...core.search import SearchQuery


class MikanSearchService:
    """蜜柑计划检索服务，内置内存结果缓存、RSS 快速订阅直达与字幕组偏好排序。"""

    def __init__(
            self,
            client: MikanClient,
            result_limit: int = 80,
            config: Optional[Dict[str, Any]] = None,
    ):
        self._client = client
        self._result_limit = max(1, min(int(result_limit or 80), 200))
        self._config = config or {}
        self._cache: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
        self._lock = RLock()

    def _cached_search(self, keyword: str) -> List[Dict[str, Any]]:
        with self._lock:
            cached = self._cache.get(keyword)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            rows = self._client.search_html(keyword)
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[keyword] = (time.monotonic(), rows)
            return rows

    def _try_rss_fast_match(self, query: SearchQuery) -> Optional[List[Dict[str, Any]]]:
        """优化对 Mikan RSS 快速订阅匹配（仅在订阅 URL 或元数据明确包含 Mikan 专属参数时尝试）。"""
        candidates_to_check = []
        if query.subscribe:
            for attr in ("url", "rss_url", "name", "custom_words"):
                val = getattr(query.subscribe, attr, None)
                if val and ("mikan" in str(val).lower() or "bangumiId=" in str(val) or "subgroupid=" in str(val)):
                    candidates_to_check.append(str(val))
        if query.mediainfo:
            meta = getattr(query.mediainfo, "source_meta", {}) or {}
            if isinstance(meta, dict) and meta.get("mikan_id"):
                candidates_to_check.append(f"bangumiId={meta['mikan_id']}")

        for text in candidates_to_check:
            rss_params = extract_mikan_rss_params(text)
            if rss_params:
                bangumi_id, subgroup_id = rss_params
                logger.debug(
                    f"[MIKAN] 命中快速订阅 RSS 匹配：bangumiId={bangumi_id}, subgroupid={subgroup_id}"
                )
                try:
                    rss_url = text if "://" in text and "RSS/Bangumi" in text else None
                    rows = self._client.fetch_rss(
                        bangumi_id=bangumi_id,
                        subgroup_id=subgroup_id,
                        rss_url=rss_url,
                    )
                    season_num = normalize_season(query.season)
                    results = []
                    for row in rows:
                        row_title = str(row.get("title") or "")
                        if not release_season_matches(row_title, season_num):
                            continue
                        episodes = release_episodes(row_title, season_num)
                        results.append({
                            **row,
                            "season": season_num,
                            "episodes": episodes,
                            "preview_episodes": {str(season_num): episodes},
                            "is_pack": is_pack_release(row_title, episodes),
                        })
                    if results:
                        logger.debug(
                            f"[MIKAN] 快速订阅 RSS 获取成功，条目数={len(results)}"
                        )
                        return results
                except Exception as exc:
                    logger.warning(f"[MIKAN] 快速订阅 RSS 请求失败，回退常规搜索：{exc}")
                    break
        return None

    def _search_bangumi_episodes(
            self,
            bangumi_id: str,
            season_num: int,
            fansub_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按番剧 ID 优先获取完整发布条目，解决分页截断问题。"""
        results = []
        # 1. 优先尝试直接拉取番剧全量 RSS（速度最快且无分页限制）
        try:
            rss_rows = self._client.fetch_rss(bangumi_id=bangumi_id, fansub_name=fansub_name)
            if rss_rows:
                for row in rss_rows:
                    row_title = str(row.get("title") or "")
                    if not release_season_matches(row_title, season_num):
                        continue
                    episodes = release_episodes(row_title, season_num)
                    results.append({
                        **row,
                        "season": season_num,
                        "episodes": episodes,
                        "preview_episodes": {str(season_num): episodes},
                        "is_pack": is_pack_release(row_title, episodes),
                    })
                return results
        except Exception as e:
            logger.debug(f"[MIKAN] 拉取番剧 RSS={bangumi_id} 失败，尝试分页展开：{e}")

        # 2. 尝试从番剧主页读取各字幕组并调用 ExpandEpisodeTable 展开完整表格
        try:
            subgroups = self._client.get_bangumi_subgroups(bangumi_id)
            for sg in subgroups:
                sg_id = sg.get("subgroup_id")
                sg_name = sg.get("name")
                if not sg_id:
                    continue
                try:
                    expanded_rows = self._client.expand_episode_table(
                        bangumi_id=bangumi_id,
                        subgroup_id=sg_id,
                        take=sg.get("take") or 65,
                        fansub_name=sg_name,
                    )
                    for row in expanded_rows:
                        row_title = str(row.get("title") or "")
                        if not release_season_matches(row_title, season_num):
                            continue
                        episodes = release_episodes(row_title, season_num)
                        results.append({
                            **row,
                            "season": season_num,
                            "episodes": episodes,
                            "preview_episodes": {str(season_num): episodes},
                            "is_pack": is_pack_release(row_title, episodes),
                        })
                except Exception as ex:
                    logger.debug(f"[MIKAN] 展开字幕组={sg_id} 失败：{ex}")
        except Exception as err:
            logger.debug(f"[MIKAN] 获取番剧字幕组失败：{err}")

        return results

    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        is_list_mode = bool(getattr(query, "resource_list_mode", False))
        strict_filter = not is_list_mode

        # 1. 优先尝试 Mikan RSS 快速订阅匹配
        rss_results = self._try_rss_fast_match(query)
        if rss_results:
            normalized = normalize_magnets(rss_results, "mikan")
            before = len(normalized)
            normalized = filter_fansubs(
                normalized, config=self._config, prefix="mikan", strict=strict_filter
            )
            if is_list_mode:
                logger.info(
                    f"[MIKAN] 快速订阅字幕组排序（列表模式全量返回）：{before} → {len(normalized)}"
                )
                return normalized
            logger.info(
                f"[MIKAN] 快速订阅字幕组排序（strict={strict_filter}）：{before} → {len(normalized)}"
            )
            limit = (
                min(self._result_limit, query.result_limit)
                if getattr(query, "result_limit", None)
                else self._result_limit
            )
            return normalized[:limit]

        # 2. 收集搜索标题候选
        explicit_keyword = str(getattr(query, "keyword", "") or "").strip()
        base_titles = unique_texts(
            ([explicit_keyword] if explicit_keyword else [])
            + ([getattr(query.subscribe, "name", "")] if getattr(query, "subscribe", None) else [])
            + media_titles(query.mediainfo)
            + ([getattr(query, "title", "")] if getattr(query, "title", None) else [])
            + ([getattr(query, "original_title", "")] if getattr(query, "original_title", None) else [])
        )
        aliases = media_aliases(query.mediainfo)
        expanded_titles = unique_texts(base_titles + aliases)
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
        season_num = normalize_season(query.season)

        # 3. 优先探测是否命中 Mikan 相关推荐番剧（Bangumi 精准关联）
        try:
            bangumis = self._client.search_bangumis(titles[0])
            for bgm in bangumis[:3]:
                bgm_id = bgm.get("bangumi_id")
                bgm_title = bgm.get("title", "")
                if bgm_id and release_matches(bgm_title, titles, query.season, fuzzy=True):
                    logger.debug(
                        f"[MIKAN] 命中相关推荐番剧：id={bgm_id}, title={bgm_title}，执行字幕组展开拉取"
                    )
                    bgm_episodes = self._search_bangumi_episodes(bgm_id, season_num)
                    results.extend(bgm_episodes)
        except Exception as bgm_error:
            logger.debug(f"[MIKAN] 探测相关推荐番剧异常：{bgm_error}")

        # 4. 常规关键词 HTML 搜索作为补充与兜底
        for keyword in search_keyword_candidates(base_titles, aliases):
            try:
                rows = self._cached_search(keyword)
            except MikanClientError as e:
                logger.warning(f"[MIKAN] 关键词={keyword} 请求异常：{e}")
                continue

            matched = [
                row for row in rows
                if release_matches(row.get("title", ""), titles, query.season, fuzzy=True)
            ]
            for row in matched:
                row_title = str(row.get("title") or "")
                if not release_season_matches(row_title, season_num):
                    continue
                episodes = release_episodes(row_title, season_num)
                results.append({
                    **row,
                    "season": season_num,
                    "episodes": episodes,
                    "preview_episodes": {str(season_num): episodes},
                    "is_pack": is_pack_release(row_title, episodes),
                })
            logger.debug(
                f"[MIKAN] 关键词={keyword}，返回={len(rows)}，匹配={len(matched)}"
            )

        normalized = normalize_magnets(results, "mikan")
        before = len(normalized)
        normalized = filter_fansubs(
            normalized, config={**self._config, "_target_season": query.season}, prefix="mikan", strict=strict_filter
        )
        if before != len(normalized):
            logger.debug(f"[MIKAN] 字幕过滤与排序（strict={strict_filter}）：{before} -> {len(normalized)}")

        if is_list_mode:
            logger.info(f"[MIKAN] 列表模式不限制数量，全量返回候选={len(normalized)}")
            return normalized

        limit = (
            min(self._result_limit, query.result_limit)
            if getattr(query, "result_limit", None)
            else self._result_limit
        )
        logger.info(f"[MIKAN] 去重后候选={len(normalized)}，返回上限={limit}")
        return normalized[:limit]

    def clear_cache(self) -> int:
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count
