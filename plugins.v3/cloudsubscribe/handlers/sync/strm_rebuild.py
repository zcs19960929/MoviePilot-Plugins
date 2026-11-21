"""
按网盘目录重建 STRM：补齐缺失文件、修正内容过期文件。

与转存流程的区别：本服务不移动、不改名任何网盘文件，只读取目录并生成/重写
本地 STRM。用于「模板或域名变更后批量修正」「历史缺失补齐」等维护场景。
"""
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.log import logger

from ...core import OwnerDelegator
from ...core.cloud import CloudFile
from ...utils import MediaFileParser


class StrmRebuildService(OwnerDelegator):
    """扫描指定网盘目录，按当前模板重建 STRM。"""

    _SAMPLE_LIMIT = 5
    _OVERWRITE_MODES = ("missing", "stale", "all")
    _MAX_DEPTH = 6
    _PROGRESS_STEP = 100

    # ------------------------------------------------------------------ 校验

    def _validate(self, cloud_path: str) -> str:
        """返回空字符串表示通过，否则为不可执行的原因。"""
        if not getattr(self, "_strm_generate_enabled", False):
            return "STRM 直接生成未启用，请先在插件配置中开启"
        if not getattr(self, "_strm_generator", None):
            return "当前网盘不支持播放引用，无法生成 STRM"
        if not str(getattr(self, "_local_resource_path", "") or "").strip():
            return "未配置本地媒体根路径"
        raw = str(cloud_path or "").strip()
        if not raw:
            return "缺少 cloud_path 参数"
        if not raw.startswith("/"):
            return f"网盘目录必须以 / 开头：{raw}"
        root = str(getattr(self, "_CLOUD_MEDIA_ROOT", "/") or "/").rstrip("/") or "/"
        normalized = raw.rstrip("/") or "/"
        if root != "/" and normalized != root and not normalized.startswith(f"{root}/"):
            return f"目录不在网盘媒体根路径 {root} 下：{normalized}"
        return ""

    # ------------------------------------------------------------------ 遍历

    def _walk_cloud_videos(
            self,
            cloud_path: str,
            limit: int,
            max_depth: int = None,
    ) -> Tuple[List[Tuple[str, CloudFile]], int]:
        """递归收集视频文件，返回 ([(所在目录, 文件)], 读取失败的目录数)。"""
        directories = getattr(self, "_cloud_directories", None)
        if not directories:
            return [], 0
        depth_limit = self._MAX_DEPTH if max_depth is None else int(max_depth)
        collected: List[Tuple[str, CloudFile]] = []
        failed_dirs = 0
        stack: List[Tuple[str, int]] = [(cloud_path.rstrip("/") or "/", 0)]
        while stack and len(collected) < limit:
            current, depth = stack.pop()
            try:
                lookup = directories.resolve_directory(current)
                if not lookup.checked:
                    failed_dirs += 1
                    logger.warning(f"重建 STRM：无法确认目录是否存在，已跳过 {current}")
                    continue
                if lookup.directory_id is None:
                    continue
                listing = directories.list_directory(lookup.directory_id)
                if not listing.checked:
                    failed_dirs += 1
                    logger.warning(f"重建 STRM：列出目录失败，已跳过 {current}")
                    continue
            except Exception as error:  # noqa: BLE001 - 单个目录失败不应中断整体扫描
                failed_dirs += 1
                logger.warning(f"重建 STRM：读取目录异常 {current} - {error}")
                continue
            for item in listing.files:
                if item.is_directory:
                    if depth < depth_limit:
                        stack.append((f"{current.rstrip('/')}/{item.name}", depth + 1))
                    continue
                if not MediaFileParser.is_video(item.name):
                    continue
                collected.append((current, item))
                if len(collected) >= limit:
                    break
        return collected, failed_dirs

    # ------------------------------------------------------------------ 判定

    def _expected(self, cloud_dir: str, file_item: CloudFile) -> Tuple[Path, str]:
        """按当前模板计算期望的本地路径与内容，不写盘。"""
        generator = self._strm_generator
        template_values = self._playback_reference.reference_values(file_item)
        cloud_file_path = generator._cloud_file_path(cloud_dir, file_item.name)
        content = generator.render(
            file_name=file_item.name,
            file_path=cloud_file_path,
            template_values=template_values,
        )
        local_path = generator.local_path(
            local_root=self._local_resource_path,
            cloud_root=self._CLOUD_MEDIA_ROOT,
            cloud_dir=cloud_dir,
            file_name=file_item.name,
        )
        return local_path, content

    @staticmethod
    def _classify(strm_path: Path, expected_content: str) -> str:
        """返回 missing / empty / stale / ok。"""
        if not strm_path.is_file():
            return "missing"
        try:
            if strm_path.stat().st_size <= 0:
                return "empty"
            existing = strm_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return "stale"
        if existing.strip() != str(expected_content).strip():
            return "stale"
        return "ok"

    # ------------------------------------------------------------------ 主流程

    def rebuild(
            self,
            cloud_path: str,
            *,
            recursive: bool = True,
            dry_run: bool = True,
            overwrite: str = "stale",
            limit: int = 5000,
    ) -> dict:
        """按目录重建 STRM。"""
        mode = str(overwrite or "stale").strip().lower()
        if mode not in self._OVERWRITE_MODES:
            return {
                "success": False,
                "message": f"overwrite 仅支持 {'/'.join(self._OVERWRITE_MODES)}",
            }
        reason = self._validate(cloud_path)
        if reason:
            return {"success": False, "message": reason}
        if getattr(self, "_strm_rebuilding", False):
            return {"success": False, "message": "已有重建任务正在执行，请稍后再试"}

        try:
            limit = max(1, min(int(limit), 20000))
        except (TypeError, ValueError):
            limit = 5000
        normalized_path = str(cloud_path).strip().rstrip("/") or "/"
        self._strm_rebuilding = True
        stats = {
            "success": True,
            "cloud_path": normalized_path,
            "overwrite": mode,
            "dry_run": bool(dry_run),
            "scanned": 0,
            "missing": 0,
            "empty": 0,
            "stale": 0,
            "ok": 0,
            "written": 0,
            "failed": 0,
            "skipped_by_mode": 0,
            "failed_directories": 0,
            "truncated": False,
            "samples": [],
            "message": "",
        }
        try:
            videos, failed_dirs = self._walk_cloud_videos(
                normalized_path, limit, self._MAX_DEPTH if recursive else 0
            )
            stats["failed_directories"] = failed_dirs
            stats["truncated"] = len(videos) >= limit
            logger.info(
                f"重建 STRM 开始：{normalized_path}，模式={mode}，"
                f"预演={'是' if dry_run else '否'}，待处理视频 {len(videos)} 个"
            )
            for index, (cloud_dir, file_item) in enumerate(videos, start=1):
                stats["scanned"] += 1
                try:
                    strm_path, expected_content = self._expected(cloud_dir, file_item)
                except Exception as error:  # noqa: BLE001 - 单文件异常不中断
                    stats["failed"] += 1
                    logger.warning(
                        f"重建 STRM：计算目标路径失败 {file_item.name} - {error}"
                    )
                    continue
                state = self._classify(strm_path, expected_content)
                stats[state] = int(stats.get(state, 0)) + 1
                should_write = (
                    mode == "all"
                    or (mode == "missing" and state in ("missing", "empty"))
                    or (mode == "stale" and state in ("missing", "empty", "stale"))
                )
                if not should_write:
                    stats["skipped_by_mode"] += 1
                    continue
                if len(stats["samples"]) < self._SAMPLE_LIMIT:
                    stats["samples"].append(
                        {"path": str(strm_path), "state": state}
                    )
                if dry_run:
                    continue
                written = self._generate_strm(
                    cloud_dir,
                    file_item.name,
                    target_file=file_item,
                    lookup_target=False,
                    log_success=False,
                )
                if written:
                    stats["written"] += 1
                else:
                    stats["failed"] += 1
                if index % self._PROGRESS_STEP == 0:
                    logger.info(
                        f"重建 STRM 进度：{index}/{len(videos)}，"
                        f"已写入 {stats['written']}"
                    )
            pending = stats["missing"] + stats["empty"] + stats["stale"]
            stats["pending"] = pending
            stats["message"] = (
                f"{'预演完成' if dry_run else '重建完成'}："
                f"扫描 {stats['scanned']}，缺失 {stats['missing']}，"
                f"空文件 {stats['empty']}，过期 {stats['stale']}，"
                f"正常 {stats['ok']}，"
                + (
                    f"待写入 {pending}"
                    if dry_run
                    else f"已写入 {stats['written']}，失败 {stats['failed']}"
                )
            )
            logger.info(f"重建 STRM 结果：{stats['message']}")
            return stats
        finally:
            self._strm_rebuilding = False
