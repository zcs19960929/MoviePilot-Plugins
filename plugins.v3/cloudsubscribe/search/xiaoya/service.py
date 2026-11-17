"""小雅搜索服务：编排本地索引检索并输出标准候选。"""

from __future__ import annotations

from itertools import zip_longest
from typing import Any, Dict, Iterable, List, Mapping

from app.log import logger

from .preview import XiaoyaPreviewResolver
from .resource import XiaoyaResourceService
from ..magnet import media_titles
from ...core.search import (
    SearchCandidate,
    SearchQuery,
    format_search_log_prefix,
    normalize_search_candidate,
)


class XiaoyaSearchService:
    """搜索编排与候选输出。"""

    def __init__(
            self,
            resources: XiaoyaResourceService,
            resource_types: Iterable[str],
            result_limit: int = 20,
            drive_registry: Any = None,
    ):
        self._resources = resources
        self._resource_types = set(resource_types)
        self._result_limit = max(1, min(int(result_limit or 20), 100))
        self._preview = XiaoyaPreviewResolver(resources, drive_registry)

    def search(self, query: SearchQuery) -> List[SearchCandidate]:
        """纯本地检索索引，输出标准候选列表。"""
        prefix = format_search_log_prefix(query, "xiaoya")
        mediainfo = query.mediainfo
        douban_id = str(getattr(mediainfo, "douban_id", "") or "").strip()
        media_type = str(getattr(query.media_type, "value", query.media_type) or "").lower()
        category = "movie" if media_type in ("movie", "电影") else "tv"

        keywords: List[str] = []
        if query.keyword and query.keyword.strip():
            keywords.append(query.keyword.strip())
        keywords.extend(t for t in media_titles(mediainfo) if t and t not in keywords)
        if not keywords and not douban_id:
            logger.debug(f"{prefix} 缺少关键词与豆瓣ID，跳过检索")
            return []

        limit = query.result_limit or self._result_limit if query.resource_list_mode else self._result_limit

        rows: List[Dict[str, Any]] = []
        seen = set()
        list_mode = bool(query.resource_list_mode)
        for keyword in keywords:
            # 季号优先精确匹配，其次退化为片名
            targets = [keyword]
            if query.season:
                targets = [f"{keyword} 第{query.season}季", f"{keyword} S{query.season:02d}", keyword]
            for target in targets:
                for item in self._resources.search(
                        target,
                        limit=limit,
                        drive_types=self._resource_types,
                        douban_id=douban_id,
                        category=category,
                ):
                    path = item.get("path")
                    if path and path not in seen:
                        seen.add(path)
                        rows.append(item)
                if not list_mode and len(rows) >= limit:
                    break
            if not list_mode and len(rows) >= limit:
                break

        # 列表模式（测试搜索/资源弹窗）按网盘类型轮转取样，
        # 避免单一网盘刷屏，保证各网盘分组 tab 都有内容
        rows = self._interleave_by_drive(rows) if list_mode else rows

        candidates = [self._to_candidate(row) for row in rows[:limit]]
        logger.info(f"{prefix} 小雅检索完成，命中有效可转存网盘资源 {len(candidates)} 条")
        return candidates

    @staticmethod
    def _interleave_by_drive(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按网盘类型轮转合并，保持各分组均有代表。"""
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(str(row.get("drive_type") or ""), []).append(row)
        if len(buckets) < 2:
            return rows
        return [row for group in zip_longest(*buckets.values()) for row in group if row]

    @staticmethod
    def _to_candidate(row: Dict[str, Any]) -> SearchCandidate:
        """索引行 → 标准候选，精确按数据库内容索引呈现单部影视。"""
        from pathlib import Path
        drive_type = str(row.get("drive_type") or "").lower()
        share_url = str(row.get("share_url") or "").strip()
        title = str(row.get("title") or "").strip()
        path = str(row.get("path") or "").strip()
        category = str(row.get("category") or "").upper()
        share_code = str(row.get("share_code") or "")
        sub_path = str(row.get("sub_path") or "").strip()
        target_name = str(row.get("target_name") or Path(sub_path or path).name or "").strip()

        # 卡片标题：精确展示数据库索引命中的具体影视条目与版本
        if title and target_name and target_name not in title and title not in target_name:
            display_title = f"{title} · {target_name}"
        else:
            display_title = title or target_name or path

        target_desc = sub_path or path
        return normalize_search_candidate({
            "title": display_title,
            "url": share_url,
            "resource_type": drive_type,
            "source": "xiaoya",
            "source_url": share_url,
            "description": f"小雅索引资源 · {drive_type.upper()}{f' · {category}' if category else ''} · 目标: {target_desc}",
            "is_free": True,
            "is_unlocked": True,
            "supports_file_preview": True,
            "can_preview": True,
            "resource_ref": share_code or path,
            "provider_data": {
                "path": path,
                "drive_type": drive_type,
                "share_code": share_code,
                "folder_id": row.get("folder_id"),
                "password": row.get("password"),
                "sub_path": sub_path,
                "target_name": target_name,
                "title": title,
                "resource_id": share_code,
            },
        }, "xiaoya")

    def preview_files(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """按本地索引命中目标展示文件列表（渠道自有数据形态，不套用分享目录遍历）。"""
        return self._preview.preview_files(payload)

    def rebuild_index(self) -> Dict[str, Any]:
        """清空并全量重建本地索引库。"""
        return self._resources.rebuild_index()

    def clear_cache(self) -> int:
        """重载内存分享缓存。"""
        return self._resources.clear_cache()
