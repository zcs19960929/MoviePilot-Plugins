"""转存完成后的媒体服务器入库通知与媒体信息提取。"""

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import RLock, Timer
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set

from app.chain.mediaserver import MediaServerChain

try:
    from app.helper.mediaserver import MediaServerHelper
except ImportError:
    from app.application.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas import MediaInfo, RefreshMediaItem
from app.schemas.types import MediaType
from app.utils.http import RequestUtils


class MediaServerResolver:
    """通过 MoviePilot 平台接口读取各媒体服务器的实际入库内容。"""

    _name_filters: Optional[List[str]] = None

    @classmethod
    def configure(cls, name_filters: Optional[List[str]]) -> None:
        cls._name_filters = list(name_filters or []) or None

    @staticmethod
    def _services() -> Dict[str, Any]:
        return MediaServerHelper().get_services(
            name_filters=MediaServerResolver._name_filters
        ) or {}

    @staticmethod
    def _stream_rule_title(path: str, container: str, streams: list) -> str:
        """组合媒体项已提供的流信息；无流详情时保留文件名。"""
        video = next((
            value for value in streams
            if isinstance(value, dict) and str(value.get("Type") or value.get("type") or "").lower() == "video"
        ), {})
        audio = next((
            value for value in streams
            if isinstance(value, dict) and str(value.get("Type") or value.get("type") or "").lower() == "audio"
        ), {})
        values = [Path(str(path or "")).stem]
        values.extend((
            container,
            video.get("DisplayTitle") or video.get("Title"),
            video.get("Codec"),
            video.get("VideoRangeType") or video.get("VideoRange"),
            f"{video.get('BitDepth')}bit" if video.get("BitDepth") else "",
            audio.get("DisplayTitle") or audio.get("Title"),
            audio.get("Codec"),
        ))
        return " ".join(dict.fromkeys(
            str(value).strip() for value in values if str(value or "").strip()
        ))

    @staticmethod
    def _platform_item_media(
            mediaserver_chain: MediaServerChain,
            server_name: str,
            item_id: str,
    ) -> Dict[str, Any]:
        item = mediaserver_chain.iteminfo(server=server_name, item_id=item_id)

        def value(*names: str, default: Any = None) -> Any:
            if isinstance(item, dict):
                for name in names:
                    if item.get(name) is not None:
                        return item.get(name)
                return default
            for name in names:
                result = getattr(item, name, None)
                if result is not None:
                    return result
            return default

        path = str(value("path", "Path", default="") or "").strip() if item else ""
        if not path:
            return {}
        try:
            size = max(0, int(value("size", "Size", default=0) or 0))
        except (TypeError, ValueError):
            size = 0
        if not size and Path(path).suffix.lower() != ".strm":
            try:
                size = Path(path).stat().st_size
            except OSError:
                pass
        container = str(value("container", "Container", default="") or "").strip() or Path(path).suffix.lstrip(".")
        streams = value("media_streams", "MediaStreams", "mediaStreams", default=[]) or []
        streams = [stream if isinstance(stream, dict) else stream.model_dump()
                   for stream in streams if isinstance(stream, dict) or hasattr(stream, "model_dump")]
        return {
            "path": path,
            "size": size,
            "item_id": str(item_id),
            "rule_title": MediaServerResolver._stream_rule_title(path, container, streams),
            "container": container,
            "media_streams": streams,
        }

    @staticmethod
    def _episode_ids(
            mediaserver_chain: MediaServerChain,
            server_name: str,
            item_id: str,
            season: int,
    ) -> Dict[int, str]:
        episode_ids = mediaserver_chain.get_season_episode_ids(
            server=server_name, item_id=item_id, season=season
        )
        if episode_ids:
            return {int(episode): str(value) for episode, value in episode_ids.items()}
        for season_info in mediaserver_chain.episodes(
                server=server_name, item_id=item_id
        ) or []:
            if int(getattr(season_info, "season", -1) or -1) != int(season):
                continue
            return {
                int(episode): ""
                for episode in (getattr(season_info, "episodes", None) or [])
            }
        return {}

    @staticmethod
    def episode_media(
            chain, mediainfo: MediaInfo, season: int
    ) -> tuple[bool, Dict[int, Dict[str, Any]]]:
        """返回媒体服务器逐集路径和大小；不读取网盘。"""
        services = MediaServerResolver._services()
        if not services or not chain or not mediainfo:
            return False, {}

        mediaserver_chain = MediaServerChain()
        result: Dict[int, Dict[str, Any]] = {}
        checked = False
        for server_name, service in services.items():
            if service.instance.is_inactive():
                continue
            try:
                exists_media = chain.media_exists(mediainfo=mediainfo, server=server_name)
                checked = True
                if not exists_media or not exists_media.itemid:
                    continue
                episode_ids = MediaServerResolver._episode_ids(
                    mediaserver_chain, server_name, str(exists_media.itemid), season
                )
                missing = [
                    (int(episode), str(item_id))
                    for episode, item_id in (episode_ids or {}).items()
                    if item_id and int(episode) not in result
                ]
                if missing:
                    with ThreadPoolExecutor(
                            max_workers=min(6, len(missing)),
                            thread_name_prefix="cloudsubscribe-media-baseline",
                    ) as executor:
                        media_items = executor.map(
                            lambda value: (
                                value[0],
                                MediaServerResolver._platform_item_media(
                                    mediaserver_chain, server_name, value[1]
                                ),
                            ),
                            missing,
                        )
                        result.update(media_items)
            except Exception as error:
                logger.warning(
                    f"读取媒体服务器洗版基线失败：{server_name} - "
                    f"{mediainfo.title_year} S{season:02d}，原因：{error}"
                )
        return checked, {episode: value for episode, value in result.items() if value.get("path")}

    @staticmethod
    def episode_snapshot(
            chain, mediainfo: MediaInfo, season: int
    ) -> tuple[bool, Dict[int, str]]:
        """返回是否成功检查过媒体服务器，以及实际存在的剧集路径。"""
        checked, media = MediaServerResolver.episode_media(chain, mediainfo, season)
        return checked, {
            episode: str(value.get("path") or "") for episode, value in media.items()
        }

    @staticmethod
    def episode_paths(chain, mediainfo: MediaInfo, season: int) -> Dict[int, str]:
        _, paths = MediaServerResolver.episode_snapshot(chain, mediainfo, season)
        return paths

    @staticmethod
    def movie_paths(chain, mediainfo: MediaInfo) -> list[str]:
        """读取媒体服务器中已入库电影的实际文件路径。"""
        services = MediaServerResolver._services()
        if not services or not chain or not mediainfo:
            return []

        paths = []
        for server_name, service in services.items():
            if service.instance.is_inactive():
                continue
            try:
                exists_media = chain.media_exists(
                    mediainfo=mediainfo,
                    server=server_name,
                )
                if not exists_media or not exists_media.itemid:
                    continue
                item_path = str(
                    MediaServerResolver._platform_item_media(
                        MediaServerChain(), server_name,
                        str(exists_media.itemid)
                    ).get("path") or ""
                ).strip()
                if item_path and item_path not in paths:
                    paths.append(item_path)
            except Exception as error:
                logger.warning(
                    f"读取媒体服务器电影洗版基线失败：{server_name} - "
                    f"{mediainfo.title_year}，原因：{error}"
                )
        return paths

    @staticmethod
    def movie_media(chain, mediainfo: MediaInfo) -> list[Dict[str, Any]]:
        """读取媒体服务器电影路径和大小，作为网盘查询前的首选基线。"""
        services = MediaServerResolver._services()
        if not services or not chain or not mediainfo:
            return []
        result = []
        for server_name, service in services.items():
            if service.instance.is_inactive():
                continue
            try:
                exists_media = chain.media_exists(mediainfo=mediainfo, server=server_name)
                if not exists_media or not exists_media.itemid:
                    continue
                media = MediaServerResolver._platform_item_media(
                    MediaServerChain(), server_name,
                    str(exists_media.itemid)
                )
                if media.get("path") and media not in result:
                    result.append(media)
            except Exception as error:
                logger.warning(
                    f"读取媒体服务器电影洗版基线失败：{server_name} - "
                    f"{mediainfo.title_year}，原因：{error}"
                )
        return result

    @staticmethod
    def episode_numbers(
            chain, mediainfo: MediaInfo, season: int
    ) -> tuple[bool, Set[int]]:
        """通过平台接口读取各媒体服务器季集清单。"""
        services = MediaServerResolver._services()
        if not services or not chain or not mediainfo:
            return False, set()

        mediaserver_chain = MediaServerChain()
        checked = False
        episodes: Set[int] = set()
        for server_name, service in services.items():
            if service.instance.is_inactive():
                continue
            try:
                exists_media = chain.media_exists(
                    mediainfo=mediainfo,
                    server=server_name,
                )
                checked = True
                if not exists_media or not exists_media.itemid:
                    continue
                episode_ids = MediaServerResolver._episode_ids(
                    mediaserver_chain, server_name,
                    str(exists_media.itemid), season
                )
                episodes.update(int(episode) for episode in (episode_ids or {}))
            except Exception as error:
                logger.warning(
                    f"读取媒体服务器剧集清单失败：{server_name} - "
                    f"{mediainfo.title_year} S{season:02d}，原因：{error}"
                )
        return checked, episodes


class MediaServerNotifier:
    """按媒体项通知所选媒体服务器，并可触发 Emby 提取媒体信息。"""

    _BATCH_WINDOW_SECONDS = 2
    _REFRESH_TIMEOUT_SECONDS = 60
    _MEDIAINFO_TIMER_LIMIT = 256
    _EMBY_REFRESH_DEDUPE_SECONDS = 15
    _EMBY_REFRESH_RETRY_DELAYS = (0, 1, 2)
    _EMBY_RETRY_STATUS_CODES = {500, 502, 503, 504}

    def __init__(
            self,
            enabled: bool = False,
            mediaservers: Optional[List[str]] = None,
            path_mappings: str = "",
            delay_seconds: int = 0,
            emby_mediainfo_enabled: bool = False,
    ):
        self.enabled = bool(enabled)
        self.mediaservers = list(mediaservers or [])
        self.path_mappings = str(path_mappings or "")
        self.delay_seconds = max(0, int(delay_seconds or 0))
        self.emby_mediainfo_enabled = bool(emby_mediainfo_enabled)
        self._batch_lock = RLock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._batch_timer: Optional[Timer] = None
        self._task_batch_depth = 0
        self._closed = False
        self._batch_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cloudsubscribe-media-batch"
        )
        self._refresh_executor = ThreadPoolExecutor(
            max_workers=max(1, min(len(self.mediaservers) or 1, 4)),
            thread_name_prefix="cloudsubscribe-media-refresh",
        )
        self._emby_refresh_recent: Dict[str, float] = {}
        self._mediainfo_timers: Dict[str, Timer] = {}

    def begin_task_batch(self) -> bool:
        """记录任务批次；通知仍按延迟窗口独立提交。"""
        with self._batch_lock:
            if self._closed:
                return False
            self._task_batch_depth += 1
        return True

    def finish_task_batch(self) -> bool:
        """结束任务批次，不绕过已配置的通知延迟。"""
        with self._batch_lock:
            if self._task_batch_depth <= 0:
                return True
            self._task_batch_depth -= 1
            if self._pending and not self._batch_timer:
                self._schedule_flush_locked()
        return True

    def _schedule_flush_locked(self) -> None:
        """在持有 _batch_lock 的情况下重置并启动批次提交定时器。"""
        is_first_timer = self._batch_timer is None
        if self._batch_timer:
            self._batch_timer.cancel()
        wait_seconds = max(self.delay_seconds, self._BATCH_WINDOW_SECONDS)
        self._batch_timer = Timer(wait_seconds, self._flush_pending)
        self._batch_timer.daemon = True
        self._batch_timer.start()
        if is_first_timer:
            logger.debug(f"入库通知批次已更新，静默 {wait_seconds} 秒后提交")

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = str(path or "").replace("\\", "/")
        return normalized.rstrip("/") or "/"

    def _media_server_path(self, moviepilot_path: Path) -> Path:
        source = self._normalize_path(str(moviepilot_path))
        matches = []
        for line in self.path_mappings.splitlines():
            parts = [part.strip() for part in line.split("#", 1)]
            if len(parts) != 2 or not all(parts):
                continue
            server_root, moviepilot_root = map(self._normalize_path, parts)
            if source == moviepilot_root or source.startswith(f"{moviepilot_root}/"):
                matches.append((len(moviepilot_root), server_root, moviepilot_root))
        if not matches:
            return Path(source)

        _, server_root, moviepilot_root = max(matches, key=lambda item: item[0])
        relative = source[len(moviepilot_root):].lstrip("/")
        translated = f"{server_root}/{relative}" if relative else server_root
        return Path(translated)

    def media_server_path(self, moviepilot_path: Path) -> Path:
        """将 MoviePilot 可访问路径转换为媒体服务器路径。"""
        return self._media_server_path(moviepilot_path)

    def notify_deleted_path(self, path: Path, record: Dict[str, Any]) -> bool:
        """按已删除历史记录的路径刷新媒体服务器目录。"""
        try:
            media_type = MediaType(str(record.get("type") or ""))
        except ValueError:
            media_type = None
        media_info = SimpleNamespace(
            title=str(record.get("title") or record.get("file_name") or ""),
            year=record.get("year"),
            type=media_type,
            category=record.get("category"),
            tmdb_id=record.get("tmdb_id"),
        )
        return self.notify(
            path,
            media_info,
            file_name=str(record.get("file_name") or ""),
            force=True,
            deleted=True,
        )

    def notify(
            self,
            path: Path,
            mediainfo: MediaInfo,
            file_name: str = "",
            force: bool = False,
            deleted: bool = False,
    ) -> bool:
        if not self.enabled and not force:
            return True
        if not self.mediaservers:
            logger.warning("入库通知已启用，但尚未选择媒体服务器")
            return False
        if not path or not mediainfo:
            logger.warning(f"入库通知缺少媒体路径或识别信息：{file_name}")
            return False

        target_path = self._media_server_path(path)
        target_folder = target_path.parent if target_path.suffix else target_path
        tmdb_id = getattr(mediainfo, "tmdb_id", None) or getattr(mediainfo, "tmdbid", None)
        title = str(getattr(mediainfo, "title", "") or "").strip()
        item = RefreshMediaItem(
            title=title,
            year=getattr(mediainfo, "year", None),
            type=getattr(mediainfo, "type", None),
            category=getattr(mediainfo, "category", None),
            target_path=target_folder,
        )
        key = self._normalize_path(str(target_folder))
        with self._batch_lock:
            if self._closed:
                logger.warning("入库通知器已关闭，无法继续接收通知")
                return False
            pending = self._pending.get(key)
            if pending:
                pending["paths"].add(target_path)
                if deleted:
                    pending.setdefault("deleted_paths", set()).add(target_path)
                if tmdb_id and not pending.get("tmdb_id"):
                    pending["tmdb_id"] = tmdb_id
            else:
                self._pending[key] = {
                    "item": item,
                    "title": title,
                    "tmdb_id": tmdb_id,
                    "folder": target_folder,
                    "local_path": path,
                    "paths": {target_path},
                    "deleted_paths": {target_path} if deleted else set(),
                }
            self._schedule_flush_locked()
        return True

    def _take_pending(self) -> List[Dict[str, Any]]:
        with self._batch_lock:
            entries = list(self._pending.values())
            self._pending.clear()
            self._batch_timer = None
        return entries

    def _flush_pending(self) -> bool:
        return self._submit_batch_async(self._take_pending())

    def _submit_batch_async(self, entries: List[Dict[str, Any]]) -> bool:
        if not entries:
            return True
        try:
            self._batch_executor.submit(self._submit_batch, entries)
        except RuntimeError:
            logger.warning("媒体库刷新执行器已关闭，无法提交新批次")
            return False
        return True

    def _refresh_service(
            self,
            name: str,
            service: Any,
            items: List[RefreshMediaItem],
            entries: List[Dict[str, Any]],
    ) -> bool:
        started_at = time.monotonic()
        logger.debug(f"开始刷新媒体库：{name} - {len(items)} 个媒体目录")
        if service.type == "emby":
            future = self._refresh_executor.submit(
                self._refresh_emby_entries, name, service, entries
            )
        else:
            future = self._refresh_executor.submit(
                service.instance.refresh_library_by_items, items
            )
        try:
            result = future.result(timeout=self._REFRESH_TIMEOUT_SECONDS)
            # MoviePilot Plex refresh 在提交 HTTP 请求后返回 None，属于正常确认
            success = bool(result) or (service.type == "plex" and result is None)
        except FutureTimeoutError:
            future.cancel()
            logger.error(
                f"媒体库刷新超时：{name} - 等待 "
                f"{self._REFRESH_TIMEOUT_SECONDS} 秒仍未返回，订阅任务不受影响"
            )
            return False
        except Exception as error:
            logger.error(
                f"媒体库刷新失败：{name} - {len(items)} 个媒体目录，"
                f"耗时 {time.monotonic() - started_at:.2f} 秒，原因：{error}"
            )
            return False

        elapsed = time.monotonic() - started_at
        if not success:
            logger.error(
                f"媒体库刷新失败：{name} - {len(items)} 个媒体目录，"
                f"耗时 {elapsed:.2f} 秒，媒体服务器未确认刷新请求"
            )
            return False
        logger.debug(
            f"媒体库刷新完成：{name} - {len(items)} 个媒体目录，"
            f"耗时 {elapsed:.2f} 秒"
        )
        return True

    def _submit_batch(self, entries: List[Dict[str, Any]]) -> bool:
        if not entries:
            return True
        services = MediaServerHelper().get_services(name_filters=self.mediaservers)
        if not services:
            logger.warning("未找到已选择的媒体服务器实例，无法提交入库通知")
            return False

        items = [entry["item"] for entry in entries]
        paths = list(
            dict.fromkeys(
                path
                for entry in entries
                for path in entry.get("paths", set())
            )
        )
        deleted_paths = {
            path
            for entry in entries
            for path in entry.get("deleted_paths", set())
        }
        mediainfo_paths = [path for path in paths if path not in deleted_paths]
        refreshed = 0
        submitted = 0
        started_at = time.monotonic()
        for name, service in services.items():
            if service.instance.is_inactive():
                logger.warning(f"媒体服务器未连接，跳过入库通知：{name}")
                continue
            if not hasattr(service.instance, "refresh_library_by_items"):
                logger.warning(f"媒体服务器不支持按项入库通知：{name}")
                continue
            submitted += 1
            if self._refresh_service(name, service, items, entries):
                refreshed += 1
                if self.emby_mediainfo_enabled and service.type == "emby":
                    logger.debug(
                        f"媒体库通知：{name} 已刷新，启动 Emby 神医媒体信息提取"
                    )
                    for path in mediainfo_paths:
                        self._schedule_emby_mediainfo(name, path, attempt=1)
                elif self.emby_mediainfo_enabled:
                    logger.debug(
                        f"媒体库通知：{name} 已刷新；该媒体库不支持 Emby 神医媒体信息提取"
                    )
        if submitted:
            logger.info(
                f"媒体库刷新批次完成：{len(paths)} 个文件，成功 {refreshed}/{submitted}，"
                f"耗时 {time.monotonic() - started_at:.2f} 秒"
            )
        return refreshed > 0

    def close(self, flush: bool = True) -> None:
        """停止定时器；配置重载或插件停止时后台提交尚未发送的批次。"""
        with self._batch_lock:
            self._closed = True
            self._task_batch_depth = 0
            timer = self._batch_timer
            self._batch_timer = None
            if timer:
                timer.cancel()
            entries = list(self._pending.values()) if flush else []
            self._pending.clear()
            self._emby_refresh_recent.clear()
            mediainfo_timers = list(self._mediainfo_timers.values())
            self._mediainfo_timers.clear()
        for mediainfo_timer in mediainfo_timers:
            mediainfo_timer.cancel()
        if entries:
            try:
                self._batch_executor.submit(self._submit_final_batch, entries)
            except RuntimeError:
                self._refresh_executor.shutdown(wait=False, cancel_futures=True)
        else:
            self._refresh_executor.shutdown(wait=False, cancel_futures=True)
        self._batch_executor.shutdown(wait=False, cancel_futures=False)

    def _submit_final_batch(self, entries: List[Dict[str, Any]]) -> None:
        try:
            self._submit_batch(entries)
        finally:
            self._refresh_executor.shutdown(wait=False, cancel_futures=True)

    @classmethod
    def _emby_connection(cls, name: str):
        service = MediaServerHelper().get_service(name=name, type_filter="emby")
        if not service or service.instance.is_inactive():
            return None
        connection = cls._emby_refresh_connection(service)
        user_id = str(service.instance.get_user() or "").strip()
        if not connection or not user_id:
            return None
        host, api_key = connection
        return host, api_key, user_id

    @staticmethod
    def _emby_refresh_connection(service: Any):
        """读取 Emby 刷新所需连接信息，不依赖用户 ID。"""
        config = service.config.config or {}
        instance = service.instance
        host = str(
            config.get("host") or getattr(instance, "_host", "") or ""
        ).strip().rstrip("/")
        api_key = str(
            config.get("apikey") or getattr(instance, "_apikey", "") or ""
        ).strip()
        if not host or not api_key:
            return None
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return host, api_key

    def _find_emby_item_id(
            self,
            host: str,
            api_key: str,
            tmdb_id: Optional[Any] = None,
            title: Optional[str] = None,
    ) -> Optional[str]:
        """按 TMDB ID 或标题精准快速定位 Emby 媒体条目 ID。"""
        # 1. 优先使用 TMDB ID（最精准，100% 对应）
        if tmdb_id:
            try:
                with RequestUtils(timeout=10).get_res(
                        url=f"{host}/emby/Items",
                        params={
                            "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
                            "Recursive": "true",
                            "IncludeItemTypes": "Series,Movie",
                            "Fields": "Path,ProviderIds",
                            "api_key": api_key,
                        },
                ) as response:
                    if response and response.status_code == 200:
                        items = (response.json() or {}).get("Items", [])
                        if items and items[0].get("Id"):
                            return str(items[0]["Id"])
            except Exception as e:
                logger.debug(f"Emby 按 TMDB ID 查询异常：{tmdb_id} - {e}")

        # 2. 次选按名称查询
        clean_title = str(title or "").strip()
        if clean_title:
            try:
                with RequestUtils(timeout=10).get_res(
                        url=f"{host}/emby/Items",
                        params={
                            "SearchTerm": clean_title,
                            "IncludeItemTypes": "Series,Movie",
                            "Recursive": "true",
                            "Fields": "Path",
                            "Limit": 5,
                            "api_key": api_key,
                        },
                ) as response:
                    if response and response.status_code == 200:
                        items = (response.json() or {}).get("Items", [])
                        if items and items[0].get("Id"):
                            return str(items[0]["Id"])
            except Exception as e:
                logger.debug(f"Emby 按标题查询异常：{clean_title} - {e}")

        return None

    @staticmethod
    def _entry_fully_deleted(entry: Dict[str, Any]) -> bool:
        """通知中出现无扩展名的目录路径（整目录已删）才可移除媒体库条目。"""
        deleted_paths = [str(path or "") for path in (entry.get("deleted_paths") or ())]
        return bool(deleted_paths) and any(
            not Path(path).suffix for path in deleted_paths if path
        )

    @classmethod
    def _delete_emby_item(cls, host: str, api_key: str, item_id: str) -> bool:
        """从 Emby 媒体库移除条目；批量删除接口对管理员 API Key 可用。"""
        try:
            response = RequestUtils(timeout=15).post_res(
                url=f"{host}/emby/Items/Delete",
                headers={"X-Emby-Token": api_key},
                params={"Ids": item_id},
            )
            status_code = getattr(response, "status_code", None) if response else None
            if status_code in {200, 204}:
                return True
            logger.warning(f"Emby 条目删除失败 (Item {item_id})：HTTP {status_code}")
            return False
        except Exception as error:
            logger.warning(f"Emby 条目删除异常 (Item {item_id})：{error}")
            return False

    @classmethod
    def _refresh_emby_item(cls, host: str, api_key: str, item_id: str) -> bool:
        """递归 FullRefresh 单个 Emby 条目，由 Emby 自行清理已删文件（无需额外配置）。"""
        try:
            response = RequestUtils(timeout=15).post_res(
                url=f"{host}/emby/Items/{item_id}/Refresh",
                headers={"X-Emby-Token": api_key},
                params={
                    "Recursive": "true",
                    "MetadataRefreshMode": "FullRefresh",
                    "ImageRefreshMode": "FullRefresh",
                    "ReplaceAllMetadata": "false",
                    "ReplaceAllImages": "false",
                },
            )
            status_code = getattr(response, "status_code", None) if response else None
            if status_code in {200, 204}:
                return True
            logger.warning(f"Emby 条目刷新失败 (Item {item_id})：HTTP {status_code}")
            return False
        except Exception as error:
            logger.warning(f"Emby 条目刷新异常 (Item {item_id})：{error}")
            return False


    def _refresh_emby_entries(
            self,
            name: str,
            service: Any,
            entries: List[Dict[str, Any]],
    ) -> bool:
        """精简直达：按媒体条目递归刷新 Emby，杜绝全库扫描。"""
        connection = self._emby_refresh_connection(service)
        if not connection:
            logger.warning(f"Emby 刷新配置无效：{name}")
            return False
        host, api_key = connection

        success_count = 0
        total_targets = 0
        handled_items: set = set()
        missing_items: Dict[str, Any] = {}

        for entry in entries:
            is_deleted = bool(entry.get("deleted_paths"))
            tmdb_id = entry.get("tmdb_id")
            title = entry.get("title") or getattr(entry.get("item"), "title", "")

            item_id = self._find_emby_item_id(
                host=host,
                api_key=api_key,
                tmdb_id=tmdb_id,
                title=title,
            )
            # 同一媒体可能出现在多条删除记录中（多集/多路径），按条目去重
            if item_id and item_id in handled_items:
                logger.debug(
                    f"Emby ({name}) 同一媒体条目已处理，跳过重复：{title} (Item ID: {item_id})"
                )
                continue
            if item_id:
                handled_items.add(item_id)

            if not item_id:
                if is_deleted:
                    # 同一媒体可能对应多条删除记录（多集/多路径），合并为一条汇总日志
                    key = str(tmdb_id or title or "").strip().lower()
                    record = missing_items.setdefault(key, {"title": title, "count": 0})
                    record["count"] += 1
                elif hasattr(service.instance, "refresh_library_by_items"):
                    try:
                        service.instance.refresh_library_by_items([entry["item"]])
                        success_count += 1
                    except Exception as err:
                        logger.warning(f"Emby ({name}) 原生单项入库刷新失败：{err}")
                continue

            total_targets += 1
            if self._entry_fully_deleted(entry) and self._delete_emby_item(host, api_key, item_id):
                success_count += 1
                logger.info(
                    f"已删除 Emby ({name}) 媒体库条目：{title} (Item ID: {item_id})"
                )
                continue
            if self._refresh_emby_item(host, api_key, item_id):
                success_count += 1
                logger.info(
                    f"已请求 Emby ({name}) "
                    f"{'删除后重扫' if is_deleted else '单项'}刷新："
                    f"{title} (Item ID: {item_id})"
                )

        for record in missing_items.values():
            title = record["title"]
            count = record["count"]
            logger.debug(
                f"Emby ({name}) 中未找到媒体条目（或已被移除）：{title}"
                + (f"（{count} 条记录已合并）" if count > 1 else "")
            )
        if total_targets:
            logger.info(f"Emby ({name}) 媒体处理完成：成功 {success_count}/{total_targets}")
        else:
            logger.info(
                f"Emby ({name}) 媒体处理完成：成功 {success_count} 个，无命中的待刷新条目"
            )
        return True

    def _schedule_emby_mediainfo(
            self, name: str, path: Path, attempt: int
    ) -> None:
        key = f"{name}\0{path.as_posix()}"

        def trigger() -> None:
            with self._batch_lock:
                self._mediainfo_timers.pop(key, None)
                if self._closed:
                    return
            self._trigger_emby_mediainfo(name, path, attempt)

        timer = Timer(
            10 if attempt == 1 else 15,
            trigger,
        )
        timer.daemon = True
        with self._batch_lock:
            if self._closed:
                return
            previous = self._mediainfo_timers.get(key)
            if previous:
                previous.cancel()
            elif len(self._mediainfo_timers) >= self._MEDIAINFO_TIMER_LIMIT:
                oldest_key = next(iter(self._mediainfo_timers))
                self._mediainfo_timers.pop(oldest_key).cancel()
                logger.warning(
                    "Emby 媒体信息提取等待队列已达上限，已丢弃最早任务"
                )
            self._mediainfo_timers[key] = timer
            timer.start()

    def _trigger_emby_mediainfo(
            self, name: str, path: Path, attempt: int
    ) -> None:
        connection = self._emby_connection(name)
        if not connection:
            logger.warning(f"Emby 媒体信息提取配置无效或服务未连接：{name}")
            return
        host, api_key, user_id = connection
        file_path = path.as_posix()
        headers = {"X-Emby-Token": api_key}
        try:
            with RequestUtils(timeout=15).get_res(
                    url=f"{host}/emby/Items",
                    headers=headers,
                    params={
                        "Path": file_path,
                        "Recursive": "true",
                        "Fields": "Path",
                        "IncludeItemTypes": "Movie,Episode,Folder,Series",
                    },
            ) as response:
                items = response.json().get("Items", []) if response else []
            item_id = next(
                (
                    item.get("Id")
                    for item in items
                    if str(item.get("Path") or "").replace("\\", "/")
                       == file_path
                ),
                None,
            )
            if not item_id:
                if attempt < 4:
                    self._schedule_emby_mediainfo(name, path, attempt + 1)
                else:
                    logger.warning(
                        f"Emby 入库后仍未找到媒体项，跳过媒体信息提取：{name} - {file_path}"
                    )
                return
            with RequestUtils(timeout=30).post_res(
                    url=f"{host}/emby/Items/{item_id}/PlaybackInfo",
                    headers=headers,
                    params={
                        "AutoOpenLiveStream": "true",
                        "IsPlayback": "true",
                        "UserId": user_id,
                    },
            ) as response:
                success = bool(response and response.status_code == 200)
            if success:
                logger.debug(f"Emby 媒体信息提取已触发：{name} - {file_path}")
            else:
                status_code = getattr(response, "status_code", None)
                logger.warning(
                    f"Emby 媒体信息提取失败：{name} - {file_path}，状态码：{status_code}"
                )
        except Exception as error:
            logger.warning(f"Emby 媒体信息提取异常：{name} - {file_path}，原因：{error}")
            if attempt < 4:
                self._schedule_emby_mediainfo(name, path, attempt + 1)
