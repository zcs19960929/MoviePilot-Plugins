"""同步任务完成通知聚合与推送服务。"""

import copy
import threading
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas.types import NotificationType

from ...core import OwnerDelegator


class SyncNotificationService(OwnerDelegator):
    """负责转存与洗版完成后的通知合并、静默窗口等待与聚合发送。"""

    _NOTIFICATION_BATCH_WINDOW_SECONDS = 3.0

    def __init__(self, owner=None):
        super().__init__(owner)
        self._notification_batch_lock = threading.RLock()
        self._notification_batch: List[Dict[str, Any]] = []
        self._notification_batch_timer: Optional[threading.Timer] = None

    def update_notification_config(
            self,
            notify: bool,
            notification_type: NotificationType,
            **kwargs,
    ) -> None:
        """更新通知开关与渠道类型。"""
        if hasattr(self, "_notify"):
            self._notify = bool(notify)
        if hasattr(self, "_notification_type"):
            self._notification_type = notification_type

    def send_transfer_notification(
            self, transfer_details: List[Dict[str, Any]], total_count: int
    ) -> None:
        """完成即入队，并按延迟窗口合并相邻任务通知。"""
        post_message = getattr(self, "_post_message", None)
        if not transfer_details or not post_message:
            return
        delay_seconds = getattr(self, "_notification_delay_seconds", 0)
        with self._notification_batch_lock:
            self._notification_batch.extend(copy.deepcopy(transfer_details))
            if self._notification_batch_timer:
                self._notification_batch_timer.cancel()
            wait_seconds = max(
                delay_seconds,
                self._NOTIFICATION_BATCH_WINDOW_SECONDS,
            )
            self._notification_batch_timer = threading.Timer(
                wait_seconds, self._flush_transfer_notifications
            )
            self._notification_batch_timer.daemon = True
            self._notification_batch_timer.start()
        logger.debug(
            f"完成通知已入队：{total_count} 个文件，"
            f"静默 {wait_seconds} 秒后合并发送"
        )

    def _flush_transfer_notifications(self) -> None:
        with self._notification_batch_lock:
            timer = self._notification_batch_timer
            self._notification_batch_timer = None
            if timer and timer is not threading.current_thread():
                timer.cancel()
            transfer_details = self._notification_batch
            self._notification_batch = []
        post_message = getattr(self, "_post_message", None)
        notify = getattr(self, "_notify", False)
        if not transfer_details or not post_message or not notify:
            return
        try:
            self._send_transfer_notification_now(transfer_details)
        except Exception as error:
            logger.warning(f"完成通知发送失败：{error}")

    def _send_transfer_notification_now(
            self, transfer_details: List[Dict[str, Any]]
    ) -> None:
        """按普通转存、跨盘转存和洗版分别发送聚合后的完成通知。"""
        post_message = getattr(self, "_post_message", None)
        if not post_message:
            return
        notification_type = getattr(self, "_notification_type", NotificationType.Plugin)

        kind_config = {
            "transfer": ("【网盘订阅助手】转存完成", "转存"),
            "cross_transfer": ("【网盘订阅助手】跨盘转存完成", "跨盘转存"),
            "upgrade": ("【网盘洗版】洗版完成", "洗版"),
        }
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for detail in transfer_details:
            kind = str(detail.get("notification_kind") or "transfer")
            grouped.setdefault(kind if kind in kind_config else "transfer", []).append(detail)

        for kind, details in grouped.items():
            text_lines = []
            first_image = None
            file_count = 0
            for detail in details:
                if detail.get("type") == "电影":
                    title = detail.get("title", "未知")
                    year = detail.get("year", "")
                    text_lines.append(f"{title} ({year})")
                    file_count += 1
                else:
                    title = detail.get("title", "未知")
                    season = max(1, int(detail.get("season") or 1))
                    episodes = sorted(detail.get("episodes") or [])
                    if episodes:
                        file_count += len(episodes)
                        if len(episodes) <= 5:
                            ep_str = ", ".join(f"E{episode:02d}" for episode in episodes)
                        else:
                            ep_str = (
                                f"E{episodes[0]:02d}-E{episodes[-1]:02d} "
                                f"共{len(episodes)}集"
                            )
                        text_lines.append(f"{title} S{season:02d} {ep_str}".strip())
                    else:
                        file_count += 1
                        text_lines.append(f"{title} S{season:02d}".strip())
                if not first_image and detail.get("image"):
                    first_image = detail.get("image")
            if len(text_lines) > 10:
                text_lines = text_lines[:10]
                text_lines.append(f"... 等共 {len(details)} 项")
            notification_title, action = kind_config[kind]
            post_message(
                mtype=notification_type,
                title=notification_title,
                text=f"本次共{action} {file_count} 个文件\n\n" + "\n".join(text_lines),
                image=first_image,
            )
            logger.info(
                f"{action}完成通知已发送：{file_count} 个文件，"
                f"{len(details)} 个媒体项"
            )
