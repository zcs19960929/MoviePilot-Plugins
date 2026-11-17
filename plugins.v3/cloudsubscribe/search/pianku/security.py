"""片库风控协议：HTTP 会话内完成人机验证与资源流解析。"""

import json
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from app.log import logger
from curl_cffi import requests

from ...utils.cache import create_platform_ttl_cache
from ...utils.cloudflare import (
    BrowserPageSession,
    mount_turnstile_page,
    solve_turnstile_via_api,
    turnstile_page_token,
)

GATE_PATH = "/openapi/resources/turnstile_gate.php"
FETCH_PATH = "/openapi/resources/fetch.php"
_VOD_ID_RE = re.compile(r"/voddetail/(\d+)")


class PiankuGateError(RuntimeError):
    """片库人机验证或资源获取失败。"""


class PiankuVerificationError(PiankuGateError):
    """片库人机验证失效，需要重新验证（区别于瞬时网络故障）。"""


#: 同站点+代理共用铸造锁，避免并发各铸一次。
_MINT_LOCKS: Dict[str, threading.Lock] = {}
_MINT_LOCKS_GUARD = threading.Lock()


def _mint_lock(identity: str) -> threading.Lock:
    """取共享铸造锁（同身份串行铸造）。"""
    with _MINT_LOCKS_GUARD:
        lock = _MINT_LOCKS.get(identity)
        if lock is None:
            lock = threading.Lock()
            _MINT_LOCKS[identity] = lock
        return lock


class PiankuGate:
    """片库协议：HTTP 会话内完成人机验证、资源流解析与链接交付。"""

    #: 单次 SSE 取数的整体超时（秒）。
    STREAM_TIMEOUT = 30.0
    #: 验证状态在会话内的复用时长（秒）。
    SESSION_TTL = 600.0
    #: 页面上 Turnstile 状态变量名（浏览器兜底铸造时使用）。
    TURNSTILE_STATE = "piankuVerification"
    #: 共享验证会话缓存键。
    _SHARED_SESSION_KEY = "verified-session"

    def __init__(
            self,
            base_url: str,
            proxy: Any = None,
            timeout: int = 30,
            gate_timeout: int = 45,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.proxy = proxy
        self.timeout = max(5, int(timeout or 30))
        self.gate_timeout = max(15, min(int(gate_timeout or 45), 120))
        self._lock = threading.RLock()
        self._session = self._new_session()
        self._verified_until = 0.0
        self._browser: Optional[BrowserPageSession] = None
        self._session_cache = self._build_session_cache()

    def _cache_identity(self) -> str:
        """跨实例共享会话的身份键。"""
        return f"{self.base_url}|{str(self.proxy or '').strip()}"

    def _build_session_cache(self):
        """验证结果跨实例共享，避免重复铸造。"""
        return create_platform_ttl_cache(
            "pianku_gate",
            self._cache_identity(),
            maxsize=8,
            ttl=int(self.SESSION_TTL),
        )

    def update_config(
            self,
            base_url: Optional[str] = None,
            proxy: Any = None,
            timeout: Optional[int] = None,
            gate_timeout: Optional[int] = None,
    ) -> None:
        """更新站点/代理/超时；站点或代理变化时重建会话与验证状态。"""
        rebuild = False
        if base_url is not None:
            normalized = str(base_url or "").strip().rstrip("/")
            rebuild = rebuild or normalized != self.base_url
            self.base_url = normalized
        if proxy is not None:
            rebuild = rebuild or proxy != self.proxy
            self.proxy = proxy
        if timeout is not None:
            self.timeout = max(5, int(timeout or 30))
        if gate_timeout is not None:
            self.gate_timeout = max(15, min(int(gate_timeout or 45), 120))
        if rebuild:
            with self._lock:
                self._session = self._new_session()
                self._verified_until = 0.0
                self._session_cache = self._build_session_cache()

    def close(self) -> None:
        """关闭会话与浏览器兜底资源。"""
        with self._lock:
            session, self._session = self._session, None
            browser, self._browser = self._browser, None
        for closer in (session, browser):
            if closer is None:
                continue
            try:
                closer.close()
            except Exception:
                pass

    def session_material(self, ttl: float = 600.0) -> Dict[str, Any]:
        """返回当前会话的 Cookie 与 UA，供并发直连请求复用。"""
        with self._lock:
            session = self._session
        if session is None or not self._verified_until:
            return {}
        if time.monotonic() >= self._verified_until + ttl:
            return {}
        return {
            "cookies": {
                item.name: item.value for item in session.cookies.jar
            },
            "user_agent": str(session.headers.get("User-Agent") or ""),
            "obtained_at": self._verified_until - self.SESSION_TTL,
        }

    def clear_material(self) -> None:
        """凭据失效（如 403）时丢弃验证状态，下次重新过盾。"""
        with self._lock:
            self._verified_until = 0.0
        try:
            self._session_cache.delete(self._SHARED_SESSION_KEY)
        except Exception as error:
            logger.debug(f"片库共享验证会话清理失败：{error}")

    def reverify(self) -> None:
        """强制重新过盾（会话凭据被站点拒绝后调用）。"""
        with self._lock:
            self._verified_until = 0.0
        self.ensure_verified()

    def _export_cookies(self, session: Any) -> List[Dict[str, Any]]:
        """导出会话 Cookie（含域路径）。"""
        rows: List[Dict[str, Any]] = []
        try:
            jar = session.cookies.jar
        except Exception:
            return rows
        for item in jar:
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                continue
            rows.append({
                "name": name,
                "value": str(getattr(item, "value", "") or ""),
                "domain": str(getattr(item, "domain", "") or ""),
                "path": str(getattr(item, "path", "/") or "/"),
                "secure": bool(getattr(item, "secure", False)),
            })
        return rows

    def _publish_shared_session(self) -> None:
        """写入共享验证会话。"""
        with self._lock:
            session = self._session
        if session is None:
            return
        cookies = self._export_cookies(session)
        if not cookies:
            return
        try:
            self._session_cache.set(self._SHARED_SESSION_KEY, {
                "cookies": cookies,
                "user_agent": str(session.headers.get("User-Agent") or ""),
                "deadline": time.time() + self.SESSION_TTL,
            })
        except Exception as error:
            logger.debug(f"片库共享验证会话写入失败：{error}")

    def _restore_shared_session(self) -> float:
        """恢复共享验证会话，返回剩余秒数。"""
        with self._lock:
            session = self._session
        if session is None:
            return 0.0
        try:
            material = self._session_cache.get(self._SHARED_SESSION_KEY)
        except Exception as error:
            logger.debug(f"片库共享验证会话读取失败：{error}")
            return 0.0
        if not isinstance(material, dict):
            return 0.0
        cookies = material.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            return 0.0
        try:
            remaining = float(material.get("deadline") or 0.0) - time.time()
        except (TypeError, ValueError):
            return 0.0
        if remaining <= 1.0:
            try:
                self._session_cache.delete(self._SHARED_SESSION_KEY)
            except Exception:
                pass
            return 0.0
        try:
            session.cookies.clear()
            for row in cookies:
                name = str(row.get("name") or "").strip()
                if not name:
                    raise ValueError("Cookie 名称缺失")
                session.cookies.set(
                    name,
                    str(row.get("value") or ""),
                    domain=str(row.get("domain") or ""),
                    path=str(row.get("path") or "/"),
                    secure=bool(row.get("secure")),
                )
        except Exception as error:
            logger.debug(f"片库共享验证会话恢复失败：{error}")
            return 0.0
        user_agent = str(material.get("user_agent") or "").strip()
        if user_agent:
            session.headers["User-Agent"] = user_agent
        self._verified_until = time.monotonic() + remaining
        return remaining

    def ensure_verified(self) -> None:
        """确保当前会话已验证（优先复用共享结果）。"""
        with self._lock:
            remaining = self._verified_until - time.monotonic()
            if remaining > 0:
                logger.debug(f"片库复用已验证会话（剩余 {int(remaining)}s）")
                return
            borrowed = self._restore_shared_session()
            if borrowed > 0:
                logger.debug(
                    f"片库复用其它实例已验证会话（剩余 {int(borrowed)}s）"
                )
                return
            with _mint_lock(self._cache_identity()):
                # 等锁期间可能已被验证，二次复用。
                borrowed = self._restore_shared_session()
                if borrowed > 0:
                    logger.debug(
                        f"片库复用其它实例已验证会话（剩余 {int(borrowed)}s）"
                    )
                    return
                self._verify_and_publish()

    def _verify_and_publish(self) -> None:
        """执行完整过盾并写入共享会话。"""
        started = time.monotonic()
        status = self._gate_status()
        if not bool((status or {}).get("verified")):
            site_key = str((status or {}).get("sitekey") or "").strip()
            action = str((status or {}).get("action") or "").strip()
            if not site_key:
                raise PiankuGateError("片库人机验证参数缺失，站点结构可能已变更")
            logger.debug("片库人机验证未通过，开始铸造 Turnstile token")
            token = self._mint_token(site_key, action)
            if not token:
                raise PiankuGateError("片库人机验证未返回 token")
            self._submit_token(token)
            if not bool((self._gate_status() or {}).get("verified")):
                raise PiankuVerificationError("片库人机验证未通过")
        self._verified_until = time.monotonic() + self.SESSION_TTL
        self._publish_shared_session()
        logger.debug(
            f"片库人机验证通过（耗时 {time.monotonic() - started:.2f}s）"
        )

    def fetch_resources(self, detail_url: str) -> List[Dict[str, Any]]:
        """取某个详情页的完整资源列表（SSE 流式事件逐条收集）。"""
        target = str(detail_url or "").strip()
        if not target:
            return []
        matched = _VOD_ID_RE.search(target)
        if not matched:
            raise PiankuGateError("片库详情链接缺少 vod_id")
        try:
            self.ensure_verified()
            items = self._stream_items(matched.group(1))
        except PiankuVerificationError:
            # 仅验证失效才丢弃会话验证状态；瞬时网络故障不重新过盾。
            self.clear_material()
            raise
        rows = self.normalize_items(items, target)
        if len(rows) != len(items):
            logger.debug(f"片库过滤无效行 {len(items) - len(rows)} 条")
        return rows

    def normalize_items(
            self, items: List[Dict[str, Any]], detail_url: str = ""
    ) -> List[Dict[str, Any]]:
        """把 SSE 事件条目规范化为 ``href/title/platform/password`` 行。"""
        rows: List[Dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            link = str(item.get("link") or "").strip()
            title = str(item.get("title") or "").strip()
            if not link or not title:
                continue
            rows.append({
                "href": urljoin(self.base_url + "/", link),
                "title": title,
                "platform": str(item.get("platform") or "").strip().lower(),
                "password": str(item.get("password") or "").strip(),
                "kind": str(item.get("kind") or "").strip().lower(),
                "source_url": detail_url,
            })
        return rows

    def _new_session(self):
        session = requests.Session(impersonate="chrome")
        session.headers.update({
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{self.base_url}/",
        })
        return session

    def _request_kwargs(self, stream: bool = False) -> Dict[str, Any]:
        proxies = (self.proxy or "").strip()
        return {
            "timeout": self.timeout,
            "stream": stream,
            "allow_redirects": not stream,
            "proxies": {"http": proxies, "https": proxies} if proxies else None,
        }

    def _gate_status(self) -> Dict[str, Any]:
        with self._lock:
            session = self._session
        if session is None:
            return {}
        try:
            response = session.get(
                f"{self.base_url}{GATE_PATH}",
                headers={"Referer": f"{self.base_url}/"},
                **self._request_kwargs(),
            )
            data = response.json()
        except Exception as error:
            logger.debug(f"片库门控状态读取失败：{error}")
            return {}
        return data if isinstance(data, dict) else {}

    def _submit_token(self, token: str) -> None:
        with self._lock:
            session = self._session
        if session is None:
            raise PiankuGateError("片库会话已关闭")
        try:
            session.post(
                f"{self.base_url}{GATE_PATH}",
                json={"token": token},
                headers={
                    "Content-Type": "application/json",
                    "Referer": f"{self.base_url}/",
                },
                **self._request_kwargs(),
            )
        except Exception as error:
            raise PiankuGateError(f"片库人机验证提交失败：{error}") from error

    def _mint_token(self, site_key: str, action: str) -> str:
        """铸造一次性 Turnstile token：外部求解网关优先，浏览器兜底。"""
        page_url = f"{self.base_url}/__gate"
        token = solve_turnstile_via_api(
            page_url, site_key, action, timeout=self.gate_timeout + 15
        )
        if token:
            logger.debug("片库外部求解网关已铸造 Turnstile token")
            return token
        return self._mint_token_in_browser(page_url, site_key, action)

    def _mint_token_in_browser(self, page_url: str, site_key: str, action: str) -> str:
        session = self._browser_session()

        def load(page) -> str:
            mount_turnstile_page(page, page_url)
            return turnstile_page_token(
                page,
                site_key,
                action,
                state_name=self.TURNSTILE_STATE,
                label="Pianku",
                deadline=float(self.gate_timeout),
                click_challenge=session.click_challenge,
            )

        return session.run(load, wait_timeout=self.gate_timeout + 30.0)

    def _browser_session(self) -> BrowserPageSession:
        with self._lock:
            if self._browser is None:
                self._browser = BrowserPageSession(
                    "Pianku-Turnstile", proxy=self.proxy, timeout=self.timeout
                )
            return self._browser

    def _stream_items(self, vod_id: str) -> List[Dict[str, Any]]:
        """读取 SSE 资源流：瞬时连接错误重试一次，``error`` 事件视为验证失效。"""
        with self._lock:
            session = self._session
        if session is None:
            raise PiankuGateError("片库会话已关闭")

        items: List[Dict[str, Any]] = []
        started = time.monotonic()
        response = None
        for attempt in range(2):
            try:
                response = session.get(
                    f"{self.base_url}{FETCH_PATH}",
                    params={"vod_id": vod_id},
                    headers={
                        "Accept": "text/event-stream",
                        "Referer": f"{self.base_url}/voddetail/{vod_id}.html",
                    },
                    **self._request_kwargs(stream=True),
                )
                break
            except Exception as error:
                if attempt or items:
                    raise PiankuGateError(f"片库资源流请求失败：{error}") from error
                # 连接被中断属瞬时故障：沿用同一会话（保留验证 Cookie）重试一次。
                logger.debug(f"片库资源流连接中断，重试一次：{error}")
                time.sleep(0.5)

        try:
            for raw in response.iter_lines():
                if time.monotonic() - started > self.STREAM_TIMEOUT:
                    logger.warning("片库资源流读取超时，使用已收到的资源行")
                    break
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "error":
                    raise PiankuVerificationError(
                        str(event.get("message") or "片库人机验证失效")
                    )
                item = event.get("item")
                if isinstance(item, dict):
                    items.append(item)
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                pass
        logger.debug(
            f"片库资源列表 {len(items)} 行"
            f"（vod_id={vod_id}，耗时 {time.monotonic() - started:.2f}s）"
        )
        return items
