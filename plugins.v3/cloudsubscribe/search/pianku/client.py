"""片库搜索客户端：列表检索、资源缓存与直链解析。

过盾与人机验证协议统一由 :mod:`.security` 提供，本模块只负责 HTTP 检索、
缓存与链接解析，并复用共享请求门控做频率控制。
"""

import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

from app.log import logger

from .security import PiankuGate, PiankuGateError, PiankuVerificationError
from ...utils.cache import cached_resource_call, create_platform_ttl_cache
from ...utils.http_client import (
    RequestGate,
    gated_request,
    normalize_proxies,
    request_error_summary,
    requests,
)


class PiankuError(RuntimeError):
    """片库请求、解析或过盾失败。"""


#: 并发解析的请求并发度与分批间隔（秒）
RESOLVE_CONCURRENCY = 8
RESOLVE_INTERVAL = 0.15
#: 单次列表/解析请求的超时上限（秒）：防止挂死请求占满共享串行队列
REQUEST_TIMEOUT_CAP = 25

#: 签名链接 -> 签发它的会话 Cookie
_LINK_SESSION_CACHE = create_platform_ttl_cache(
    "pianku_link_sessions", ttl=6 * 3600, maxsize=4096
)


class PiankuClient:
    """片库搜索客户端。"""

    DEFAULT_BASE_URL = "https://4k.pianku.online"
    _HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    _ITEM_RE = re.compile(
        r'<a[^>]+href="(?:https?://[^"]*)?/voddetail/(?P<id>\d+)\.html"'
        r'[^>]*title="(?P<title>[^"]*)"[^>]*>(?P<body>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    _REMARKS_RE = re.compile(r'class="remarks"[^>]*>(?P<value>[^<]*)<', re.IGNORECASE)
    _SUBTITLE_RE = re.compile(r'class="subtitle"[^>]*>(?P<value>[^<]*)<', re.IGNORECASE)
    _OPEN_REDIRECT_RE = re.compile(
        r'location\.href\s*=\s*["\'](?P<target>[^"\']+)["\']', re.IGNORECASE
    )

    def __init__(
            self,
            base_url: str = DEFAULT_BASE_URL,
            proxy: Optional[str] = None,
            timeout: int = 30,
            request_interval: float = 1.5,
            detail_interval: float = 2.0,
            gate_timeout: int = 45,
    ) -> None:
        self.base_url = (
                str(base_url or self.DEFAULT_BASE_URL).strip().rstrip("/")
                or self.DEFAULT_BASE_URL
        )
        self._proxy = str(proxy or "").strip()
        self.timeout = max(5, int(timeout or 30))
        self.request_interval = max(0.5, float(request_interval or 1.5))
        self.detail_interval = max(0.2, float(detail_interval or 2.0))
        self.gate_timeout = max(15, int(gate_timeout or 45))
        #: 解析真实链接之间的最小间隔（不再占用公共请求门控的 ≥1s 队列）。
        self.resolve_interval = RESOLVE_INTERVAL
        self._request_gate = self._build_request_gate()
        self._page_cache = create_platform_ttl_cache("pianku_search", ttl=1800, maxsize=200)
        #: 签名与会话绑定，缓存不得超过会话有效期。
        self._resource_cache = create_platform_ttl_cache(
            "pianku_resources", ttl=int(PiankuGate.SESSION_TTL), maxsize=200
        )
        self._link_cache = create_platform_ttl_cache("pianku_links", ttl=6 * 3600, maxsize=800)
        self._lock = threading.RLock()
        #: 并发相同关键词/详情只发一次请求。
        self._cache_locks = tuple(threading.RLock() for _ in range(8))
        self._throttled = False
        self._gate_protocol: Optional[PiankuGate] = None

    @property
    def proxy(self) -> str:
        return self._proxy

    def _build_request_gate(self) -> RequestGate:
        """复用共享门控：串行、按间隔限速，代理或地址变更时重建。"""
        return RequestGate.shared(
            "Pianku",
            f"{self.base_url}|{self._proxy}",
            request_interval=self.request_interval,
            minimum_interval=0.5,
            server_error_cooldown_seconds=3,
            serial_requests=True,
        )

    @property
    def is_cooling_down(self) -> bool:
        """返回门控当前是否处于风控或服务端错误冷却状态。"""
        if self._request_gate is None:
            return False
        return self._request_gate.cooldown_remaining > 0

    def update_config(
            self,
            base_url: Optional[str] = None,
            proxy: Optional[str] = None,
            timeout: Optional[int] = None,
            request_interval: Optional[float] = None,
            detail_interval: Optional[float] = None,
            gate_timeout: Optional[int] = None,
    ) -> None:
        if base_url is not None:
            self.base_url = (
                    str(base_url).strip().rstrip("/") or self.DEFAULT_BASE_URL
            )
        if proxy is not None:
            self._proxy = str(proxy or "").strip()
        if timeout is not None:
            self.timeout = max(5, int(timeout or 30))
        if request_interval is not None:
            self.request_interval = max(0.5, float(request_interval or 1.5))
        if detail_interval is not None:
            self.detail_interval = max(0.2, float(detail_interval or 2.0))
        if gate_timeout is not None:
            self.gate_timeout = max(15, int(gate_timeout or 45))
        self._request_gate = self._build_request_gate()
        if self._gate_protocol is not None:
            self._gate_protocol.update_config(
                base_url=self.base_url,
                proxy=self._proxy,
                timeout=self.timeout,
                gate_timeout=self.gate_timeout,
            )

    def _security(self) -> PiankuGate:
        """按需创建过盾协议对象；会话与代理复用由 :class:`PiankuGate` 维护。"""
        if self._gate_protocol is None:
            self._gate_protocol = PiankuGate(
                self.base_url,
                proxy=self._proxy,
                timeout=self.timeout,
                gate_timeout=self.gate_timeout,
            )
        return self._gate_protocol

    def search_titles(self, keyword: str) -> List[Dict[str, Any]]:
        """按关键词检索详情条目（标题 / 年份 / 备注 / 详情链接）。"""
        text = str(keyword or "").strip()
        if not text:
            return []
        if self.is_cooling_down:
            logger.debug(
                f"片库门控处于冷却中（剩余 {self._request_gate.cooldown_remaining:.1f} 秒），"
                f"跳过关键词 '{text}'"
            )
            return []
        cache_key = text.casefold()
        return cached_resource_call(
            self._page_cache,
            cache_key,
            lambda: self._fetch_search_items(text),
            locks=self._cache_locks,
            access_lock=self._lock,
        )

    def _fetch_search_items(self, text: str) -> List[Dict[str, Any]]:
        """请求并解析关键词列表页。"""
        url = f"{self.base_url}/vodsearch/{quote(text)}----------1---.html"
        proxies = normalize_proxies(self._proxy)

        def _requester() -> Any:
            return requests.get(
                url,
                headers=self._HEADERS,
                proxies=proxies,
                timeout=min(self.timeout, REQUEST_TIMEOUT_CAP),
                allow_redirects=True,
                impersonate="chrome",
            )

        try:
            response = gated_request(
                self._request_gate,
                _requester,
                retry_exceptions=(
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                ),
                max_retries=2,
                initial_delay=1.5,
            )
        except Exception as error:
            raise PiankuError(f"片库列表请求失败：{request_error_summary(error)}") from error
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise PiankuError(f"片库列表请求异常：HTTP {status}")

        return [
            dict(item)
            for item in self._parse_search_page(getattr(response, "text", "") or "")
        ]

    def _parse_search_page(self, page_html: str) -> List[Dict[str, Any]]:
        """解析搜索结果列表中的详情条目。"""
        results: List[Dict[str, Any]] = []
        seen = set()
        if not page_html:
            return results
        for match in self._ITEM_RE.finditer(page_html):
            vod_id = str(match.group("id") or "").strip()
            if not vod_id or vod_id in seen:
                continue
            seen.add(vod_id)
            body = match.group("body") or ""
            remarks = self._first_group(self._REMARKS_RE, body)
            subtitle = self._first_group(self._SUBTITLE_RE, body)
            results.append({
                "id": vod_id,
                "title": html.unescape(str(match.group("title") or "")).strip(),
                "remarks": html.unescape(remarks).strip(),
                "subtitle": html.unescape(subtitle).strip(),
                "url": f"{self.base_url}/voddetail/{vod_id}.html",
            })
        return results

    @staticmethod
    def _first_group(pattern: re.Pattern, text: str) -> str:
        matched = pattern.search(text or "")
        return matched.group("value") if matched else ""

    def fetch_resources(self, vod_id: Any) -> List[Dict[str, Any]]:
        """读取某个详情页的资源列表（磁力 / 网盘分享）。"""
        key = str(vod_id or "").strip()
        if not key:
            return []
        return cached_resource_call(
            self._resource_cache,
            key,
            lambda: self._fetch_detail_throttled(key),
            locks=self._cache_locks,
            access_lock=self._lock,
        )

    def _fetch_detail_throttled(self, key: str) -> List[Dict[str, Any]]:
        """读取详情页资源并补齐最小间隔。"""
        started = time.monotonic()
        try:
            return self._fetch_detail_with_retry(key)
        except PiankuGateError as error:
            raise PiankuError(str(error)) from error
        finally:
            # 详情页已耗时数秒，仅补齐不足的最小间隔，不再叠加固定等待
            remaining = self.detail_interval - (time.monotonic() - started)
            if remaining > 0:
                self._sleep(remaining)

    def _fetch_detail_with_retry(self, key: str) -> List[Dict[str, Any]]:
        """读取详情资源列表；瞬时网络故障重试一次，验证失效直接抛出。"""
        detail_url = f"{self.base_url}/voddetail/{key}.html"
        protocol = self._security()
        for attempt in range(2):
            try:
                return protocol.fetch_resources(detail_url)
            except PiankuVerificationError:
                raise
            except PiankuGateError as error:
                if attempt:
                    raise
                logger.debug(f"片库详情 {key} 读取失败，重试一次：{error}")
                self._sleep(0.5)
        return []

    def resolve_link(self, href: str) -> str:
        """解析单个 open.php 跳转，返回真实磁力或分享链接。"""
        target_url = str(href or "").strip()
        if not target_url:
            return ""
        return self.resolve_links([target_url]).get(target_url, "")

    def resolve_links(self, hrefs: List[str]) -> Dict[str, str]:
        """批量解析 open.php：签名与签发会话绑定，按链接取回对应会话 Cookie 后并发请求。"""
        targets = list(dict.fromkeys(
            str(value or "").strip() for value in hrefs if str(value or "").strip()
        ))
        if not targets:
            return {}
        started = time.monotonic()
        self._throttled = False
        resolved_map: Dict[str, str] = {}
        pending: List[str] = []
        with self._lock:
            for target in targets:
                cached = self._link_cache.get(target)
                if cached:
                    resolved_map[target] = str(cached)
                else:
                    pending.append(target)
        if not pending:
            return resolved_map

        materials = self._link_materials(pending)
        missing = [target for target in pending if target not in materials]
        if missing:
            logger.warning(
                f"片库 {len(missing)} 条链接缺少签名会话（解析需重新搜索获取），本次跳过"
            )
            pending = [target for target in pending if target in materials]

        failed: List[str] = []
        if pending:
            workers = max(1, min(RESOLVE_CONCURRENCY, len(pending)))
            for offset in range(0, len(pending), workers):
                if self._throttled:
                    break
                chunk = pending[offset:offset + workers]
                with ThreadPoolExecutor(
                        max_workers=workers,
                        thread_name_prefix="cloudsubscribe-pianku-resolve",
                ) as executor:
                    for target, link in zip(chunk, executor.map(
                            lambda href: self._fetch_link(href, materials[href]), chunk
                    )):
                        if link:
                            resolved_map[target] = link
                        else:
                            failed.append(target)
                if self._throttled:
                    break
                if offset + workers < len(pending) and self.resolve_interval:
                    self._sleep(self.resolve_interval)
            if resolved_map:
                with self._lock:
                    for target, link in resolved_map.items():
                        self._link_cache[target] = link

        if failed and not self._throttled:
            # 凭据被站点拒绝：刷新自身会话后重试（仅对使用本实例 Cookie 的链接有效）。
            self._ensure_own_material()
            materials = self._link_materials(failed)
            retry = [target for target in failed if target in materials]
            if retry:
                resolved_map.update(self._resolve_after_reverify(retry, materials))

        unresolved = [target for target in targets if target not in resolved_map]
        if unresolved and not self._throttled:
            # 签名已过期：作废详情缓存，下一轮重取新签名。
            with self._lock:
                self._resource_cache.clear()
            logger.debug(
                f"片库 {len(unresolved)} 条链接解析被拒，已作废详情缓存以重新获取签名"
            )
        logger.debug(
            f"片库资源链接解析 {len(resolved_map)}/{len(targets)} 条"
            f"（耗时 {time.monotonic() - started:.2f}s）"
        )
        return resolved_map

    def remember_link_session(self, hrefs: Any) -> None:
        """记录签名链接对应的会话 Cookie，供其它实例（预览/转存）解析时复用。"""
        cookies = dict(self._session_material().get("cookies") or {})
        if not cookies:
            return
        for href in hrefs or ():
            target = str(href or "").strip()
            if target:
                _LINK_SESSION_CACHE.set(target, dict(cookies))

    def _link_materials(self, targets: List[str]) -> Dict[str, Dict[str, Any]]:
        """按链接组装解析凭据：优先签名会话 Cookie，其次当前实例会话。"""
        own = dict(self._session_material().get("cookies") or {})
        materials: Dict[str, Dict[str, Any]] = {}
        for target in targets:
            cached = _LINK_SESSION_CACHE.get(target)
            cookies = dict(cached) if isinstance(cached, dict) else own
            if cookies:
                materials[target] = {"cookies": cookies}
        return materials

    def _session_material(self) -> Dict[str, Any]:
        """已过盾会话凭据（Cookie/UA），按 TTL 复用。"""
        try:
            return self._security().session_material()
        except Exception as error:
            logger.debug(f"片库会话凭据获取失败：{error}")
            return {}

    def _ensure_own_material(self) -> Dict[str, Any]:
        """刷新本实例会话（重新过盾），用于自身 Cookie 被站点拒绝后的重试。"""
        protocol = self._security()
        try:
            protocol.reverify()
        except PiankuGateError as error:
            logger.warning(f"片库重新过盾失败：{error}")
            return {}
        return self._session_material()

    def _fetch_link(self, target: str, material: Dict[str, Any]) -> str:
        """宿主侧请求 open.php 并解析真实链接；凭据失效或限流返回空串。"""
        try:
            response = requests.get(
                target,
                headers=self._resolve_headers(material),
                cookies=dict(material.get("cookies") or {}),
                timeout=min(self.timeout, REQUEST_TIMEOUT_CAP),
                allow_redirects=False,
                impersonate="chrome",
            )
        except Exception as error:
            logger.debug(f"片库资源解析请求异常：{error}")
            return ""
        status = int(getattr(response, "status_code", 0) or 0)
        headers = getattr(response, "headers", None) or {}
        location = ""
        try:
            location = str(headers.get("location") or "").strip()
        except Exception:
            location = ""
        body = ""
        if status == 200 or (not location and status < 400):
            try:
                body = str(getattr(response, "text", "") or "")
            except Exception:
                body = ""
        if status in {429, 503}:
            # 被站点限流：保留会话，停止本批解析，不再触发重新过盾。
            self._throttled = True
            logger.warning(f"片库解析请求被站点限流（HTTP {status}），本批剩余解析跳过")
            return ""
        if status in {401, 403}:
            # 签名会话已失效：丢弃该链接的会话缓存，重试时改用本实例会话。
            _LINK_SESSION_CACHE.delete(target)
        return self._extract_link(status, location, body, target)

    def _resolve_headers(self, material: Dict[str, Any]) -> Dict[str, str]:
        """解析用请求头：UA 由 curl_cffi 指纹伪装提供，避免与会话指纹不一致。"""
        headers = dict(self._HEADERS)
        headers["Referer"] = f"{self.base_url}/"
        return headers

    def _resolve_after_reverify(
            self, targets: List[str], materials: Dict[str, Dict[str, Any]]
    ) -> Dict[str, str]:
        """刷新会话后重试解析（仅针对仍有签名会话或本实例会话的链接）。"""
        logger.debug(f"片库 {len(targets)} 条资源链接首次解析失败，刷新会话后重试")
        result: Dict[str, str] = {}
        for target in targets:
            if self._throttled:
                break
            material = materials.get(target)
            if not material:
                continue
            link = self._fetch_link(target, material)
            if link:
                result[target] = link
            if self.resolve_interval:
                self._sleep(self.resolve_interval)
        if result:
            with self._lock:
                for target, link in result.items():
                    self._link_cache[target] = link
        return result

    def _extract_link(self, status: int, location: str, body: str, target_url: str) -> str:
        """统一解析：优先 Location（302 跳转），其次页面内联跳转脚本。"""
        if 300 <= int(status or 0) < 400:
            return urljoin(target_url, location) if location else ""
        resolved = self._parse_open_redirect(body)
        if resolved:
            return resolved
        return urljoin(target_url, location) if location else ""

    def _parse_open_redirect(self, body: str) -> str:
        matched = self._OPEN_REDIRECT_RE.search(body or "")
        if matched:
            return html.unescape(matched.group("target")).strip()
        return ""

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, float(seconds or 0.0))
        while time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        protocol, self._gate_protocol = self._gate_protocol, None
        if protocol is not None:
            protocol.close()

    def clear_cache(self) -> int:
        """清空检索/资源列表缓存；已解析出的真实链接仍按 TTL 保留。"""
        with self._lock:
            count = len(self._page_cache) + len(self._resource_cache)
            self._page_cache.clear()
            self._resource_cache.clear()
        return count
