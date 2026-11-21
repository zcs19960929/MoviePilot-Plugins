"""按目录补齐节目图片：对缺 poster/fanart 的剧集调用 TMDB 刮削。"""
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas.types import MediaType

from .. import OwnerDelegator
from ..media import recognize_media

_SHOW_DIR_RE = re.compile(r"^(?P<title>.+?)\s*[\(（](?P<year>\d{4})[\)）]\s*$")
_SEASON_RE = re.compile(r"^season\s*\d+$", re.IGNORECASE)


class MediaImageApi(OwnerDelegator):
    """遍历指定目录，为缺图的节目补齐 TMDB 图片。"""

    _SAMPLE_LIMIT = 8

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _parse_show_dir(name: str) -> Tuple[str, Optional[int]]:
        """从「遮天 (2023)」这样的目录名解析标题与年份。"""
        raw = str(name or "").strip()
        matched = _SHOW_DIR_RE.match(raw)
        if matched:
            return matched.group("title").strip(), int(matched.group("year"))
        return raw, None

    @staticmethod
    def _season_sample(show_dir: Path) -> Optional[Tuple[Path, int]]:
        """取该剧某个季目录下的首个 .strm，供刮削推导剧根与季号。"""
        if not show_dir.is_dir():
            return None
        seasons = sorted(
            (p for p in show_dir.iterdir() if p.is_dir() and _SEASON_RE.match(p.name)),
            key=lambda p: p.name,
        )
        for season_dir in seasons:
            try:
                strm = next(season_dir.glob("*.strm"), None)
            except OSError:
                strm = None
            if strm is None:
                continue
            digits = re.sub(r"\D", "", season_dir.name)
            season = int(digits) if digits else 1
            return strm, season
        return None

    def _local_base(self, cloud_path: str, handler: Any) -> Optional[Path]:
        """把网盘路径映射为本地路径，并校验不越界。"""
        local_root = str(getattr(self, "_local_resource_path", "") or "").strip()
        cloud_root = str(getattr(handler, "_CLOUD_MEDIA_ROOT", "/") or "/").rstrip("/")
        if not local_root:
            return None
        raw = str(cloud_path or "").strip().rstrip("/") or "/"
        root = cloud_root or "/"
        if root != "/":
            if raw == root:
                relative = ""
            elif raw.startswith(f"{root}/"):
                relative = raw[len(root):].lstrip("/")
            else:
                return None
        else:
            relative = raw.lstrip("/")
        base = Path(local_root).joinpath(*[p for p in relative.split("/") if p])
        try:
            resolved = base.resolve(strict=False)
            root_resolved = Path(local_root).resolve(strict=False)
            if resolved != root_resolved and root_resolved not in resolved.parents:
                return None
        except OSError:
            return None
        return base

    # ------------------------------------------------------------------ 主流程

    def api_media_refresh_images(self, payload: Dict[str, Any]) -> dict:
        data = payload or {}
        dry_run = bool(data.get("dry_run", True))
        # 刮削器与图片开关都挂在同步处理器上，插件本体没有这些属性
        handler = getattr(self, "_sync_handler", None)
        scraper = getattr(handler, "_metadata_scraper", None) if handler else None
        if not scraper:
            return {"success": False, "message": "元数据刮削器未初始化（需开启 NFO/图片刮削）"}
        if not bool(getattr(self, "_image_scrape_enabled", False)):
            return {"success": False, "message": "图片刮削未启用，请先在插件配置中开启"}
        base = self._local_base(str(data.get("cloud_path") or ""), handler)
        if base is None:
            return {"success": False, "message": "目录不在本地媒体根路径下"}
        if not base.is_dir():
            return {"success": False, "message": f"本地目录不存在：{base}"}

        limit = int(data.get("limit") or 500)
        offset = max(0, int(data.get("offset") or 0))
        chain = MediaChainHolder.get()

        stats = {
            "success": True,
            "cloud_path": str(data.get("cloud_path") or ""),
            "local_path": str(base),
            "dry_run": dry_run,
            "scanned": 0,
            "missing": 0,
            "scraped": 0,
            "created": 0,
            "unmatched": 0,
            "failed": 0,
            "samples": [],
            "message": "",
        }

        # 分类目录 → 其下每个子目录是一部剧；若传入的本身就是剧目录，则只处理它
        candidates = [
            p for p in sorted(base.iterdir())
            if p.is_dir() and not _SEASON_RE.match(p.name)
        ]
        if not candidates:
            candidates = [base]

        batch = candidates[offset:offset + limit]
        stats["total"] = len(candidates)
        stats["offset"] = offset
        for index, show_dir in enumerate(batch, start=1):
            if _SEASON_RE.match(show_dir.name):
                continue
            stats["scanned"] += 1
            sample = self._season_sample(show_dir)
            if sample is None:
                continue
            strm_path, season = sample
            title, year = self._parse_show_dir(show_dir.name)
            if not title:
                continue
            try:
                meta = MetaInfo(title)
                meta.year = year
                meta.type = MediaType.TV
                mediainfo = recognize_media(
                    chain, meta=meta, mtype=MediaType.TV, cache=True
                )
            except Exception as error:  # noqa: BLE001 - 单条识别失败不中断
                stats["failed"] += 1
                logger.warning(f"补图识别失败：{show_dir.name} - {error}")
                continue
            if not mediainfo or not getattr(mediainfo, "tmdb_id", None):
                stats["unmatched"] += 1
                continue
            items = [{"media_path": str(strm_path), "season": season}]
            try:
                missing_items = scraper.filter_missing_items(items, mediainfo)
            except Exception as error:  # noqa: BLE001
                stats["failed"] += 1
                logger.warning(f"补图缺图判定失败：{show_dir.name} - {error}")
                continue
            if not missing_items:
                continue
            stats["missing"] += 1
            if len(stats["samples"]) < self._SAMPLE_LIMIT:
                stats["samples"].append(
                    {"show": show_dir.name, "tmdb": getattr(mediainfo, "tmdb_id", "")}
                )
            if dry_run:
                continue
            try:
                created = scraper.scrape_batch(missing_items, mediainfo)
                stats["scraped"] += 1
                stats["created"] += int(created or 0)
            except Exception as error:  # noqa: BLE001
                stats["failed"] += 1
                logger.warning(f"补图刮削失败：{show_dir.name} - {error}")
            if index % 50 == 0:
                logger.info(
                    f"补图进度：{index}/{len(candidates)}，已刮削 {stats['scraped']}"
                )

        stats["message"] = (
            f"{'预演完成' if dry_run else '补图完成'}："
            f"扫描 {stats['scanned']} 部，缺图 {stats['missing']} 部，"
            f"未匹配 TMDB {stats['unmatched']} 部，失败 {stats['failed']} 部"
            + ("" if dry_run else f"，已刮削 {stats['scraped']} 部，新增 {stats['created']} 个文件")
        )
        logger.info(f"补图结果：{stats['message']}")
        return stats


class MediaChainHolder:
    """按需创建 MediaChain，避免插件加载期就引入 app.chain。"""

    _instance: Any = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            from app.chain.media import MediaChain

            cls._instance = MediaChain()
        return cls._instance
