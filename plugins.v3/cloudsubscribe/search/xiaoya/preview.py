"""小雅资源文件预览：严格按本地索引命中的目标影视定位，不倾倒整份分享目录。

小雅索引库记录的是「某部影视在小雅挂载树中的相对路径」，而非分享根目录；
因此预览必须据此精准定位到目标文件，而不是走通用的分享链接整包目录遍历。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

from app.log import logger

from .resource import XiaoyaResourceService
from ..types import (
    PREVIEW_PROVIDER_KEYS,
    common_parent_id,
    normalize_resource_type,
    preview_item,
    resource_type_name,
)

#: 目录下钻的最大层数，避免超大分享的深路径遍历拖垮预览
_MAX_PATH_DEPTH = 8
#: 无服务端分享搜索的网盘走有界遍历，控制单次预览的请求量
_MAX_WALK_DEPTH = 3
_MAX_WALK_ITEMS = 600


class XiaoyaPreviewResolver:
    """把索引条目与网盘分享内真实文件对齐，产出精准预览结果。"""

    def __init__(
            self,
            resources: XiaoyaResourceService,
            drive_registry: Any = None,
    ):
        self._resources = resources
        self._drive_registry = drive_registry

    @staticmethod
    def target_keyword(target_name: str, title: str = "") -> str:
        """由索引条目推导分享内精准定位词：英文标准命名取首段，中文名取正名。"""
        name = str(target_name or "").strip()
        if name:
            clean = re.sub(
                r"\.(mkv|mp4|iso|ts|avi|rmvb|flv|wmv|m2ts|mov)$",
                "", name, flags=re.IGNORECASE,
            )
            parts = [seg for seg in clean.split(".") if seg]
            if len(parts) >= 2 and re.match(r"^[A-Za-z0-9]+$", parts[0]):
                return parts[0]
            clean = re.sub(r"[（(\[【]\s*(19|20)\d{2}\s*[)）\]】]", " ", clean)
            return re.sub(r"[\s._\-]+", " ", clean).strip()[:30]
        text = re.sub(r"[\(（\[【].*?[\)）\]】]", "", str(title or "")).strip()
        text = re.sub(r"[：:·\-\s/].*$", "", text).strip()
        return text

    @staticmethod
    def is_related(name: str, target_name: str, keyword: str) -> bool:
        """判断分享内条目是否属于索引命中的同一部影视。"""
        low_name = str(name or "").strip().lower()
        if not low_name:
            return False
        for token in (keyword, target_name):
            token = str(token or "").strip().lower()
            if token and token in low_name:
                return True
        prefix = str(target_name or "").strip().lower()[:18]
        return bool(prefix) and low_name.startswith(prefix)

    def _drive_service(self, drive_type: str) -> Optional[Any]:
        """取网盘的分享读取服务，未配置或未实现时返回 None。"""
        provider_key = PREVIEW_PROVIDER_KEYS.get(drive_type)
        if not provider_key or not self._drive_registry:
            return None
        try:
            _, service = self._drive_registry.require_share_transfer(provider_key)
        except RuntimeError:
            return None
        return service

    def preview_files(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """按索引定位结果返回预览数据。"""
        payload = dict(payload or {})
        provider_data = (
            dict(payload.get("provider_data") or {})
            if isinstance(payload.get("provider_data"), dict) else {}
        )
        drive_type = normalize_resource_type(
            provider_data.get("drive_type") or payload.get("resource_type")
        )
        sub_path = str(
            provider_data.get("sub_path") or provider_data.get("path") or ""
        ).strip().strip("/")
        target_name = str(provider_data.get("target_name") or "").strip()
        if not target_name:
            target_name = sub_path.rsplit("/", 1)[-1] if sub_path else ""
        title = str(provider_data.get("title") or "").strip()
        folder_id = str(provider_data.get("folder_id") or "").strip()
        url = str(payload.get("url") or "").strip()
        keyword = str(payload.get("keyword") or "").strip()

        if not url:
            return {"success": False, "message": "小雅资源缺少分享链接，无法预览"}

        service = self._drive_service(drive_type)
        if service is None:
            return self._failure(
                f"{resource_type_name(drive_type)}暂不支持预览与转存"
                if drive_type else "当前网盘类型不支持预览与转存"
            )

        try:
            # 1) 分享内服务端搜索（115）：直接定位索引命中的那部影视的真实文件
            if hasattr(service, "search_share_files"):
                locate = keyword or self.target_keyword(target_name, title)
                if locate:
                    raw_files = service.search_share_files(url, locate) or []
                    related = [
                        item for item in raw_files
                        if self.is_related(str((item or {}).get("name") or ""), target_name, locate)
                    ]
                    if related:
                        # 搜索命中的是目标目录本身时，展开其内容以便直接选中转存
                        parent_id = ""
                        breadcrumbs = None
                        if (
                            len(related) == 1
                            and related[0].get("is_dir")
                            and hasattr(service, "list_share_directory")
                        ):
                            hit_id = str(related[0].get("id") or "")
                            hit_name = str(related[0].get("name") or "").strip()
                            children = service.list_share_directory(
                                url, parent_id=hit_id
                            ) or []
                            if children:
                                related = children
                                # 记录真实层级，前端据此提供“上一级”回到分享根目录
                                parent_id = hit_id
                                breadcrumbs = [
                                    {"id": "", "name": "分享根目录"},
                                    {"id": hit_id, "name": hit_name or target_name or "当前目录"},
                                ]
                        if breadcrumbs is None:
                            # 直接命中文件时，用命中项的父目录标识补出“上一级”可回退的层级
                            parent_id = common_parent_id(related)
                            if parent_id:
                                breadcrumbs = [
                                    {"id": "", "name": "分享根目录"},
                                    {"id": parent_id, "name": target_name or locate or "当前目录"},
                                ]
                        return self._file_result(
                            drive_type, url, related, sub_path, target_name,
                            keyword=locate, searchable=True,
                            parent_id=parent_id, breadcrumbs=breadcrumbs,
                        )

            # 2) 无分享内搜索：按索引相对路径定向下钻，只列目标所在目录
            if sub_path and hasattr(service, "list_share_directory"):
                located = self._locate_by_path(service, url, sub_path, folder_id, target_name)
                if located is not None:
                    rows, breadcrumbs, parent_id = located
                    return self._file_result(
                        drive_type, url, rows, sub_path, target_name,
                        keyword="", searchable=False,
                        parent_id=parent_id, breadcrumbs=breadcrumbs,
                    )
        except Exception as error:
            # 分享失效/违规/取消等由网盘侧给出的原因必须原样返回，
            # 否则会拿索引条目冒充可预览内容，让人误以为资源可用
            reason = str(error).strip() or error.__class__.__name__
            logger.warning(f"小雅资源定位失败（{drive_type}）：{reason}")
            return self._failure(f"无法读取分享内容：{reason}")

        return self._failure("未能在分享内定位到该文件，可尝试用关键词手动搜索")

    @staticmethod
    def _failure(message: str) -> Dict[str, Any]:
        """预览失败：只回传原因，不返回任何伪造条目。"""
        return {"success": False, "message": message}

    def _locate_by_path(
            self,
            service: Any,
            url: str,
            sub_path: str,
            folder_id: str,
            target_name: str,
    ) -> Optional[list]:
        """按索引相对路径逐层下钻，返回 (目录条目, 目录链, 当前目录标识)。"""
        segments = [seg for seg in sub_path.split("/") if seg]
        if len(segments) > _MAX_PATH_DEPTH:
            segments = segments[-_MAX_PATH_DEPTH:]

        # 目录链首项固定为分享根目录，便于前端“上一级”逐层回退
        breadcrumbs = [{"id": "", "name": "分享根目录"}]
        current_id = folder_id or ""
        for seg in segments[:-1]:
            rows = service.list_share_directory(url, parent_id=current_id) or []
            matched = next(
                (
                    row for row in rows
                    if str((row or {}).get("name") or "").strip() == seg
                    and (row or {}).get("is_dir")
                ),
                None,
            )
            if not matched:
                return None
            current_id = str(matched.get("id") or "")
            breadcrumbs.append({"id": current_id, "name": seg})

        rows = service.list_share_directory(url, parent_id=current_id) or []
        leaf = segments[-1] if segments else target_name
        scoped = [
            row for row in rows
            if str((row or {}).get("name") or "").strip() == leaf
            or self.is_related(str((row or {}).get("name") or ""), target_name, "")
        ]
        if not scoped:
            return None
        target_row = scoped[0]
        if target_row.get("is_dir"):
            target_id = str(target_row.get("id") or "")
            children = service.list_share_directory(url, parent_id=target_id) or []
            if children:
                return (
                    children,
                    breadcrumbs + [{"id": target_id, "name": leaf}],
                    target_id,
                )
        return scoped, breadcrumbs, current_id

    def _file_result(
            self,
            drive_type: str,
            url: str,
            rows: Any,
            sub_path: str,
            target_name: str,
            *,
            keyword: str = "",
            searchable: bool = False,
            parent_id: str = "",
            breadcrumbs: Optional[list] = None,
    ) -> Dict[str, Any]:
        """网盘定位成功的结果封装。"""
        files = [preview_item(item) for item in rows][:200]
        label = "索引定位到"
        return {
            "success": True,
            "message": f"{label} {len(files)} 个文件",
            "data": {
                "items": files,
                "count": len(files),
                "provider_name": resource_type_name(drive_type),
                "resource_type": drive_type,
                "resource_type_name": resource_type_name(drive_type),
                "share_url": url,
                "parent_id": parent_id,
                "breadcrumbs": list(breadcrumbs or []),
                "keyword": keyword,
                "search_mode": bool(keyword),
                "searchable": bool(searchable),
                "indexed_path": sub_path,
                "target_name": target_name,
            },
        }
