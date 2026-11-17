"""现有媒体版本基线。"""

import json
import re
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Dict, List

from app.db import SessionFactory
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...core.media import (
    get_transfer_history_by,
    media_identity,
    tmdb_id_of,
)
from ...utils import MediaFileParser
from ...utils.cache import normalize_platform_cache_key


class UpgradeBaselineService(OwnerDelegator):
    """现有媒体版本基线；媒体库不可用时按历史或网盘真实文件兜底。"""

    @staticmethod
    def _copy_episode_items(
            value: Dict[int, List[Dict[str, Any]]]
    ) -> Dict[int, List[Dict[str, Any]]]:
        return {
            int(episode): [dict(item) for item in items]
            for episode, items in value.items()
        }

    @staticmethod
    def _baseline_key(subscribe, season: int) -> tuple:
        return (
            int(tmdb_id_of(subscribe) or 0),
            str(getattr(subscribe, "name", "") or "").casefold(),
            str(getattr(subscribe, "year", "") or ""),
            int(season or 1),
        )

    @staticmethod
    def _record_value(record: Any, key: str, default: Any = None) -> Any:
        if isinstance(record, dict):
            return record.get(key, default)
        return getattr(record, key, default)

    @staticmethod
    def _decode_file_item(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                return decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                pass
        return {}

    @staticmethod
    def _int_or_zero(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _episode_numbers(value: Any) -> List[int]:
        """解析的 E01、E01-E03、列表等剧集表达。"""
        if isinstance(value, (list, tuple, set)):
            return sorted({int(item) for item in value if str(item).isdigit()})
        text = str(value or "").strip()
        marked = [int(item) for item in re.findall(r"E(\d+)", text, re.I)]
        numbers = marked or [int(item) for item in re.findall(r"\d+", text)]
        if len(numbers) >= 2 and "-" in text:
            return list(range(numbers[0], numbers[-1] + 1))
        return sorted(set(numbers))

    def _transfer_history_episode_files(
            self, subscribe, season: int
    ) -> Dict[int, List[Dict[str, Any]]]:
        """一次查询整季整理历史，按集建立文件候选索引。"""
        from app.db.transferhistory_oper import TransferHistoryOper

        cache_key = normalize_platform_cache_key(
            self._baseline_key(subscribe, season)
        )
        with self._baseline_cache_lock:
            cached = self._baseline_transfer_cache.get(cache_key)
            if cached is not None:
                return self._copy_episode_items(cached)
        media_source, media_id = media_identity(subscribe)
        tmdbid = tmdb_id_of(subscribe)
        with SessionFactory() as db:
            oper = TransferHistoryOper(db=db)
            records = get_transfer_history_by(
                oper,
                mtype=MediaType.TV.value,
                tmdb_id=tmdbid,
                media_source=media_source,
                media_id=media_id,
                season=f"S{season:02d}",
            ) if media_id or tmdbid else []
            if not records:
                records = oper.get_by(
                    mtype=MediaType.TV.value,
                    title=getattr(subscribe, "name", None),
                    year=str(getattr(subscribe, "year", None) or "") or None,
                    season=f"S{season:02d}",
                ) or []

        episode_files: Dict[int, List[Dict[str, Any]]] = {}
        for record in records or []:
            file_item = self._decode_file_item(
                self._record_value(record, "src_fileitem")
                or self._record_value(record, "dest_fileitem")
            )
            file_name = str(
                file_item.get("name") or file_item.get("basename")
                or Path(str(self._record_value(record, "src", "") or "")).name
            ).strip()
            if not file_name:
                continue
            item = {
                "file_name": file_name,
                "file_size": self._int_or_zero(file_item.get("size")),
                "source": "MoviePilot整理记录",
            }
            for episode in self._episode_numbers(
                    self._record_value(record, "episodes")):
                episode_files.setdefault(episode, []).append(item)
        with self._baseline_cache_lock:
            self._baseline_transfer_cache.set(
                cache_key, self._copy_episode_items(episode_files)
            )
        return episode_files

    def _plugin_history_episode_files(
            self, subscribe, season: int
    ) -> Dict[int, List[Dict[str, Any]]]:
        """按显式 episode 字段读取插件转存历史，不依赖 STRM 文件名。"""
        cache_key = normalize_platform_cache_key(
            self._baseline_key(subscribe, season)
        )
        with self._baseline_cache_lock:
            cached = self._baseline_plugin_cache.get(cache_key)
            if cached is not None:
                return self._copy_episode_items(cached)
        tmdbid = self._int_or_zero(tmdb_id_of(subscribe))
        title = str(getattr(subscribe, "name", "") or "")
        episode_files: Dict[int, List[Dict[str, Any]]] = {}
        current_drive_name = str(getattr(getattr(self, "_cloud_drive", None), "name", "") or "").strip().lower()
        current_drive_key = str(getattr(getattr(self, "_cloud_drive", None), "key", "") or "").strip().lower()
        for record in (self._get_data("history") or []) if self._get_data else []:
            rec_drive_name = str(
                record.get("cloud_drive_name") or record.get("target_drive_name") or "").strip().lower()
            rec_drive_key = str(record.get("target_drive_key") or record.get("cloud_drive_key") or "").strip().lower()
            if rec_drive_key and current_drive_key and rec_drive_key != current_drive_key:
                continue
            if rec_drive_name and current_drive_name and rec_drive_name != current_drive_name:
                continue
            record_tmdbid = self._int_or_zero(record.get("tmdb_id"))
            if tmdbid and record_tmdbid != tmdbid:
                continue
            if not tmdbid and str(record.get("title") or "") != title:
                continue
            if self._int_or_zero(record.get("season") or 1) != season:
                continue
            if str(record.get("status") or "") in {"失败", "转存失败", "已删除"}:
                continue
            episode = self._int_or_zero(record.get("episode"))
            file_name = str(
                record.get("source_file_name") or record.get("file_name") or ""
            ).strip()
            if not episode or not file_name:
                continue
            episode_files.setdefault(episode, []).append({
                "file_name": file_name,
                "file_size": self._int_or_zero(record.get("file_size")),
                "target_file_name": str(record.get("file_name") or "").strip(),
                "cloud_dir": str(record.get("cloud_dir") or "").strip(),
                "source": "插件转存记录",
            })
        with self._baseline_cache_lock:
            self._baseline_plugin_cache.set(
                cache_key, self._copy_episode_items(episode_files)
            )
        return episode_files

    def _build_episode_baseline(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            include_saved: bool = True,
    ) -> Dict[int, Dict[str, Any]]:
        """合并整理历史、插件历史与媒体库路径；STRM 不提供文件大小。"""
        candidates = self._transfer_history_episode_files(subscribe, season)
        for episode, items in self._plugin_history_episode_files(
                subscribe, season).items():
            candidates.setdefault(episode, []).extend(items)

        manual_baseline = getattr(subscribe, "_manual_media_baseline", {}) or {}
        for episode, item in (manual_baseline.get("episodes") or {}).items():
            try:
                episode_number = int(episode)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                candidates.setdefault(episode_number, []).append(dict(item))

        media_server_key = normalize_platform_cache_key((
            *self._baseline_key(subscribe, season),
            int(getattr(mediainfo, "tmdb_id", 0) or 0),
        ))
        with self._baseline_cache_lock:
            cached_media_server = self._baseline_media_server_cache.get(
                media_server_key
            )
        if cached_media_server is None:
            _, media_server_items = self._media_server_resolver.episode_media(
                chain=self._chain,
                mediainfo=mediainfo,
                season=season,
            )
            with self._baseline_cache_lock:
                self._baseline_media_server_cache.set(media_server_key, {
                    int(episode): dict(item)
                    for episode, item in media_server_items.items()
                })
        else:
            media_server_items = {
                int(episode): dict(item)
                for episode, item in cached_media_server.items()
            }
        for episode, media_item in media_server_items.items():
            media_file = Path(str(media_item.get("path") or ""))
            file_size = self._int_or_zero(media_item.get("size"))
            if not file_size and media_file.suffix.lower() != ".strm":
                try:
                    file_size = media_file.stat().st_size
                except OSError:
                    pass
            candidates.setdefault(int(episode), []).append({
                "file_name": media_file.name,
                "rule_title": str(
                    media_item.get("rule_title") or media_file.name
                ).strip(),
                "file_size": file_size,
                "source": "媒体服务器",
            })

        baseline: Dict[int, Dict[str, Any]] = {}
        score_cache: Dict[tuple, int] = {}
        for episode, items in candidates.items():
            scored = []
            for item in items:
                rule_title = str(
                    item.get("rule_title") or item["file_name"]
                ).strip()
                score_key = (rule_title, int(item["file_size"] or 0))
                score = score_cache.get(score_key)
                if score is None:
                    score = self._get_mp_rule_score(
                        rule_title, item["file_size"], subscribe, season, mediainfo
                    )
                    score_cache[score_key] = score
                scored.append({**item, "score": score, "rule_score": score})
            if scored:
                baseline[int(episode)] = max(
                    scored,
                    key=lambda item: (
                        int(item.get("score") or 0),
                        (
                            int(int(item.get("file_size") or 0) > 0)
                            if self._upgrade_mode == "smallest" else 0
                        ),
                        (
                            -int(item.get("file_size") or 0)
                            if self._upgrade_mode == "smallest"
                            else int(item.get("file_size") or 0)
                        ),
                    ),
                )
                media_server_size = self._int_or_zero(
                    (media_server_items.get(int(episode)) or {}).get("size")
                )
                if media_server_size and not int(
                        baseline[int(episode)].get("file_size") or 0
                ):
                    baseline[int(episode)]["file_size"] = media_server_size
                    baseline[int(episode)]["size_source"] = "媒体服务器"

        if include_saved:
            for episode, score in self._read_ep_priority(subscribe).items():
                try:
                    episode_number = int(episode)
                    score_number = int(score or 0)
                except (TypeError, ValueError):
                    continue
                baseline.setdefault(episode_number, {
                    "file_name": "",
                    "file_size": 0,
                    "source": "订阅历史评分",
                    "score": score_number,
                    "rule_score": score_number,
                })
        return baseline

    def _manual_episode_baseline(
            self,
            row: Any,
            tmdb_id: int,
            media_type: Any,
            season: int,
            relative: str,
    ) -> Dict[int, Dict[str, Any]]:
        """媒体库读不到剧集时的兜底基线：统一基线（历史）→ 网盘真实文件。"""
        mediainfo = MediaInfo()
        mediainfo.type = media_type
        mediainfo.title = str(getattr(row, "title", "") or "")
        mediainfo.year = str(getattr(row, "year", "") or "") or None
        mediainfo.tmdb_id = tmdb_id
        baseline = self._build_episode_baseline(
            self._manual_baseline_subscribe(row, tmdb_id, media_type, season),
            mediainfo,
            season,
        )
        if baseline:
            return baseline
        return self._cloud_episode_baseline(row, season, relative)

    @staticmethod
    def _manual_baseline_subscribe(row: Any, tmdb_id: int, media_type: Any, season: int) -> Any:
        """媒体库条目对应的订阅占位对象，供统一基线复用订阅字段。"""
        return SimpleNamespace(
            id=0,
            name=str(getattr(row, "title", "") or ""),
            year=getattr(row, "year", None),
            tmdbid=tmdb_id,
            type=media_type,
            season=season,
            episode_priority=None,
        )

    def _cloud_episode_baseline(
            self, row: Any, season: int, relative: str
    ) -> Dict[int, Dict[str, Any]]:
        """历史也没有时，直接读取目标网盘目录的真实文件建立基线。"""
        if not self._cloud_directories:
            return {}
        cloud_root = str(getattr(self, "_CLOUD_MEDIA_ROOT", "/") or "").rstrip("/")
        base = PurePosixPath(f"{cloud_root}/{str(relative or '').strip('/')}")
        for path in (str(base / f"Season {season:02d}"), str(base)):
            valid, files = self._resolve_and_list(self._cloud_directories, path)
            if not valid:
                continue
            baseline = self._episode_baseline_from_files(files, season, path)
            if baseline:
                return baseline
        return {}

    def _episode_baseline_from_files(
            self, files: Any, season: int, cloud_dir: str
    ) -> Dict[int, Dict[str, Any]]:
        """按真实文件名解析集号，同集保留更大的文件。"""
        baseline: Dict[int, Dict[str, Any]] = {}
        for item in files or []:
            if getattr(item, "is_directory", False):
                continue
            name = str(getattr(item, "name", "") or "")
            if not MediaFileParser.is_video(name):
                continue
            size = int(getattr(item, "size", 0) or 0)
            for episode in self._parse_resource_episode_names([name], season):
                current = baseline.get(int(episode))
                if current and int(current.get("file_size") or 0) >= size:
                    continue
                baseline[int(episode)] = {
                    "file_name": name,
                    "file_size": size,
                    "source": "网盘文件",
                    "cloud_dir": cloud_dir,
                    "cloud_file_id": str(getattr(item, "id", "") or ""),
                }
        return baseline
