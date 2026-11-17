import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

from .matching import unique_texts
from ..utils.magnet import parse_magnet_metadata

_SIZE_REGEX = re.compile(r"(\d+(?:\.\d+)?)\s*(PB|TB|GB|MB|KB|B)?", re.IGNORECASE)
_MAGNET_REGEX = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}[^\s\"'<>]*", re.IGNORECASE)
_HASH_REGEX = re.compile(r"urn:btih:([a-zA-Z0-9]{32,40})", re.IGNORECASE)

SIZE_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024 ** 2,
    "GB": 1024 ** 3,
    "TB": 1024 ** 4,
    "PB": 1024 ** 5,
}


def parse_size_str(size_text: Any) -> int:
    """解析字符串大小或数值为整数字节数。

    支持例如 '1.5 GB'、'500MB'、'12,345 KB'、'1024'，或者已经是整型/浮点型的输入。
    """
    if size_text is None:
        return 0
    if isinstance(size_text, (int, float)):
        return max(0, int(size_text))
    text = str(size_text).strip().replace(",", "")
    if not text:
        return 0
    match = _SIZE_REGEX.search(text)
    if not match:
        return 0
    val = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    return int(val * SIZE_UNITS.get(unit, 1))


def extract_magnet_hash(text: str) -> Optional[str]:
    """从文本或磁力链接中提取 32 或 40 位 hex/base32 的 info_hash（统一返回大写）。"""
    if not text:
        return None
    match = _HASH_REGEX.search(text)
    if match:
        return match.group(1).upper()
    bare = re.search(r"\b([a-fA-F0-9]{40}|[A-Z2-7a-z]{32})\b", text)
    if bare:
        return bare.group(1).upper()
    return None


def extract_magnet_links(text: str) -> List[str]:
    """从文本中提取所有完整的磁力链接。"""
    if not text:
        return []
    return [match.group(0) for match in _MAGNET_REGEX.finditer(text)]


def build_magnet_url(info_hash: str, display_name: Optional[str] = None) -> str:
    """标准构建磁力链接。"""
    clean_hash = str(info_hash or "").strip().lower()
    if not clean_hash:
        return ""
    base = f"magnet:?xt=urn:btih:{clean_hash}"
    if display_name:
        clean_name = str(display_name).strip()
        if clean_name:
            return f"{base}&dn={quote(clean_name)}"
    return base


def clear_cache(target: Any) -> int:
    operation = getattr(target, "clear_cache", None)
    if not callable(operation):
        return 0
    result = operation()
    if isinstance(result, dict):
        return sum(int(value or 0) for value in result.values())
    return int(result or 0)


def media_titles(mediainfo: Any) -> List[str]:
    return unique_texts((
        getattr(mediainfo, "title", ""),
        getattr(mediainfo, "original_title", ""),
        getattr(mediainfo, "original_name", ""),
    ))


def normalize_magnets(
        resources: Iterable[Dict[str, Any]], source: str
) -> List[Dict[str, Any]]:
    normalized = []
    seen = set()
    for resource in resources or []:
        url = str(resource.get("url") or "").strip()
        if not url.casefold().startswith("magnet:?"):
            normalized.append(resource)
            continue
        provider_text = " ".join(
            str(resource.get(key) or "").strip()
            for key in (
                "title", "description", "name", "raw_title",
                "release_name", "quality",
            )
            if str(resource.get(key) or "").strip()
        )
        default_season = (
            int(resource.get("target_season"))
            if resource.get("identity_verified") and resource.get("target_season")
            else None
        )
        metadata = parse_magnet_metadata(
            url, provider_text, default_season=default_season
        )
        if not metadata:
            continue
        info_hash = str(metadata.get("info_hash") or "").upper()
        if not info_hash or info_hash in seen:
            continue
        seen.add(info_hash)
        item = dict(resource)
        item.update({
            "source": source,
            "resource_type": "magnet",
            "magnet_metadata": metadata,
            "info_hash": info_hash,
        })
        if metadata.get("display_name"):
            item["magnet_name"] = metadata["display_name"]
        if metadata.get("size") and not item.get("size"):
            item["size"] = metadata["size"]
        if metadata.get("preview_episodes"):
            item["preview_episodes"] = metadata["preview_episodes"]
        normalized.append(item)
    return normalized
