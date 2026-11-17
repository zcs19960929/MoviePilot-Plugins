"""小雅网络客户端：同步源拉取、304 条件缓存与账户卡片契约。"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from app.log import logger

from ...utils.http_client import requests, request_error_summary


class XiaoyaError(RuntimeError):
    """小雅网络或数据下载异常。"""

    def __init__(self, message: str, code: str = "xiaoya_error"):
        super().__init__(message)
        self.code = code


class XiaoyaClient:
    """远程同步源客户端，并实现账户卡片契约（get_account_info）。"""

    DEFAULT_SYNC_URL = "https://ailg.ggbond.org"

    def __init__(
            self,
            sync_url: Optional[str] = None,
            proxy: Optional[str] = None,
            timeout: int = 60,
    ):
        self.sync_url = str(sync_url or self.DEFAULT_SYNC_URL).strip().rstrip("/")
        self.proxy = proxy
        self.timeout = max(5, int(timeout or 60))
        self._resources: Optional[Any] = None

    def bind_resources(self, resources: Any) -> None:
        """关联本地资源服务供账户卡片状态读取与同步协调。"""
        self._resources = resources

    def fetch_data_file(
            self,
            filename: str,
            etag: Optional[str] = None,
            last_modified: Optional[str] = None,
    ) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
        """从配置的同步源拉取指定数据文件，支持 ETag/304 条件缓存。"""
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        headers = {"User-Agent": "MoviePilot-CloudSubscribe/1.0"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        url = f"{self.sync_url}/{filename}"
        try:
            logger.debug(f"尝试从小雅数据源拉取 {filename}：{url}")
            resp = requests.get(
                url,
                proxies=proxies,
                timeout=self.timeout,
                headers=headers,
            )
            if resp.status_code == 304:
                logger.debug(f"小雅数据文件未发生变动（HTTP 304）：{filename}")
                return None, etag, last_modified

            if resp.status_code == 200 and resp.content:
                new_etag = resp.headers.get("ETag")
                new_lm = resp.headers.get("Last-Modified")
                logger.info(
                    f"小雅数据文件拉取成功：{filename}（体积 {len(resp.content) / 1024:.1f} KB）"
                )
                return resp.content, new_etag, new_lm

            last_error = RuntimeError(f"HTTP {resp.status_code}")
        except Exception as error:
            last_error = error

        raise XiaoyaError(
            f"下载小雅数据文件 {filename} 失败：{request_error_summary(last_error)}",
            code="xiaoya_download_failed",
        )

    def get_account_info(self, force: bool = False, refresh: bool = False) -> Dict[str, Any]:
        """账户卡片契约：索引未就绪或主动刷新时同步一次，再输出收录统计。"""
        if not self._resources:
            return {"connected": False, "error": "小雅资源服务尚未初始化"}

        status = self._resources.get_status()
        if force or refresh or not status.get("is_ready"):
            try:
                self._resources.sync_index(force=True)
            except Exception as error:
                logger.warning(f"刷新小雅卡片时同步索引库失败：{error}")
            status = self._resources.get_status()

        is_ready = status.get("is_ready", False)
        last_sync = status.get("last_sync_time", 0)

        return {
            "connected": is_ready,
            "name": "小雅离线索引",
            "badge": "已就绪" if is_ready else "待初始化",
            "points_label": "已收录影视",
            "points": f"{status.get('resource_count', 0)} 部",
            "details": [
                {"label": "数据库", "value": f"{status.get('db_size_mb', 0):.2f} MB"},
                {
                    "label": "最近更新",
                    "value": time.strftime("%Y-%m-%d %H:%M", time.localtime(last_sync)) if last_sync else "未同步",
                },
            ],
            "error": status.get("error", ""),
        }
