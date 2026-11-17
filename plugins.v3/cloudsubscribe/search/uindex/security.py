"""UIndex 反盾协议：Cloudflare 质询识别与按需浏览器渲染。

UIndex 正常访问无需盾；仅在直连被 Cloudflare Managed Challenge 拦截（或直连异常）
时才按需拉起公共反盾浏览器渲染页面。本模块把这段协议从客户端中拆出，便于统一维护。
"""

from typing import Any, Optional

from ...utils.cloudflare import fetch_cloudflare_html, is_cloudflare_challenge


class UIndexSecurityError(RuntimeError):
    """UIndex 反盾获取失败。"""


class UIndexSecurity:
    """UIndex 页面获取协议：直连优先，命中 Cloudflare 盾时按需过盾渲染。"""

    DEFAULT_TIMEOUT = 35

    def __init__(self, proxy: Any = None, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.proxy = proxy
        self.timeout = max(15, int(timeout or self.DEFAULT_TIMEOUT))

    def update_config(self, proxy: Any = None, timeout: Optional[int] = None) -> None:
        if proxy is not None:
            self.proxy = proxy
        if timeout is not None:
            self.timeout = max(15, int(timeout or self.DEFAULT_TIMEOUT))

    @staticmethod
    def is_challenge(response: Any) -> bool:
        """统一判断直连响应是否命中 Cloudflare 质询页。"""
        if response is None:
            return False
        return is_cloudflare_challenge(
            str(getattr(response, "text", "") or ""),
            int(getattr(response, "status_code", 0) or 0),
            getattr(response, "headers", None) or {},
        )

    def fetch_html(self, url: str) -> str:
        """穿透 Cloudflare 盾并返回渲染后的页面 HTML。"""
        try:
            return fetch_cloudflare_html(url, proxy=self.proxy, timeout=self.timeout)
        except Exception as error:
            raise UIndexSecurityError(f"CloakBrowser 渲染页面失败：{error}") from error

    def resolve_page(
            self,
            url: str,
            response: Any = None,
            request_error: Optional[BaseException] = None,
    ) -> str:
        """直连成功且未命中盾时返回原响应；否则过盾渲染，非 200 直连响应报错。"""
        if response is not None and request_error is None and not self.is_challenge(response):
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code != 200:
                raise UIndexSecurityError(f"UIndex 请求异常：HTTP {status_code}")
            return str(getattr(response, "text", "") or "")
        return self.fetch_html(url)
