"""CloudSubscribe 公共 Cloudflare 过盾能力。

文件按依赖顺序排列（入口在最后）：

1. 平台适配：``platform_setting``（v3 运行时设置 / v2 ``settings``）
2. 常量与模板：部署侧可调的阈值、特征词与页面脚本
3. 模块单例：``_solver_service`` / ``get_cloudflare_solver``（延迟创建）
4. 质询识别：``is_cloudflare_challenge`` / ``is_cloudflare_response``
5. 代理解析与页面工具：``browser_proxy`` / ``click_challenge_frame`` 等
6. 浏览器会话：``BrowserPageSession``（按需拉起、空闲释放、线程内执行）
7. Turnstile token：``turnstile_page_token`` / ``mint_turnstile_token``
8. 外部求解服务：``CloudflareSolverService``（求解网关 / FlareSolverr）
9. 过盾入口：``solve_cloudflare_challenge`` 及会话注入、页面获取

过盾优先级：外部求解服务（本地零浏览器）→ 本地按需反盾浏览器，后者优先使用平台浏览器接口。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote, urlparse, urlsplit

from app.core.config import settings
from app.helper.browser import PlaywrightHelper
from app.log import logger
from curl_cffi import requests

try:
    from cloakbrowser import launch_context
except ImportError:
    launch_context = None

try:
    from app.sdk.browser import launch_browser_context as _platform_launch_context
except ImportError:
    _platform_launch_context = None

try:
    # v2 原生 / v3 兼容别名：平台通用的页面挑战识别（标题 + 选择器）
    from app.helper.cloudflare import under_challenge as _platform_under_challenge
except ImportError:
    _platform_under_challenge = None

from .http_client import (
    normalize_proxies,
    normalize_service_url,
    request_error_summary,
)


def platform_setting(name: str, default: Any = None) -> Any:
    """读取平台配置：v3 走运行时设置表（内部已兼容旧 settings），v2 走 ``settings``。"""
    try:
        from app.runtime.settings import get_runtime_setting
    except ImportError:
        return getattr(settings, name, default)
    try:
        return get_runtime_setting(name, default)
    except Exception:
        return getattr(settings, name, default)


#: 反盾浏览器空闲多久后自动释放（秒），避免后台常驻占用内存。
IDLE_RELEASE_SECONDS = 120.0
#: 空闲看门狗最长轮询间隔（秒）。
_WATCHDOG_MAX_INTERVAL_SECONDS = 5.0
#: 质询页正文特征。
_CHALLENGE_MARKERS = (
    "cf-chl-", "challenges.cloudflare.com", "cf-browser-verification",
    "cdn-cgi/challenge-platform", "enable javascript and cookies",
    "<title>just a moment", "verify you are human", "checking your browser",
)
#: 403/503 + server: cloudflare 时的拦截页特征。
_BLOCK_MARKERS = (
    "cloudflare", "attention required", "error 1020", "ray id:", "access denied",
)
#: 命中后才调用平台识别器的正文线索（避免无关页面的解析开销）。
_CHALLENGE_HINTS = ("cloudflare", "challenge", "ddos-guard", "cf-")
#: 外部求解服务单次求解等待上限（秒）。
_SOLVER_MAX_TIMEOUT = 120
#: 外部求解服务形态探测超时（秒）。
_SOLVER_PROBE_TIMEOUT = 8.0
#: 任务形态轮询间隔（秒）。
_SOLVER_POLL_INTERVAL = 3.0
#: 任务形态提交的求解类型（YesCaptcha / CapSolver 约定）。
_SOLVER_TASK_TYPE = "TurnstileTaskProxyless"
#: 质询页找不到验证框且长时间无进展时的重载次数上限。
_MAX_CHALLENGE_RELOADS = 1

#: 本地 Turnstile 挂载页：渲染官方组件并换取一次性 token。
TURNSTILE_MOUNT_HTML = """<!doctype html><html><head><meta charset="utf-8">
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" defer></script>
</head><body><div id="verification" style="margin:80px"></div></body></html>"""

#: 渲染并执行 Turnstile 的页面脚本，``__STATE__`` 替换为状态变量名。
_TURNSTILE_RENDER_TEMPLATE = """
({siteKey, action}) => {
    const stateName = '__STATE__';
    const previous = window[stateName];
    try {
        if (previous && previous.widget !== undefined) window.turnstile.remove(previous.widget);
    } catch (e) {}
    const state = {token: '', error: '', interactive: false};
    window[stateName] = state;
    state.widget = window.turnstile.render('#verification', {
        sitekey: siteKey, action: action || 'login', theme: 'light', language: 'zh-CN',
        appearance: 'interaction-only', execution: 'execute',
        'response-field': false,
        callback: token => { state.token = token; },
        'error-callback': code => { state.error = String(code || 'verification_failed'); },
        'expired-callback': () => { state.error = 'token_expired'; },
        'timeout-callback': () => { state.error = 'verification_timeout'; },
        'before-interactive-callback': () => { state.interactive = true; }
    });
    window.turnstile.execute(state.widget);
}
"""

#: 取出 token 后清理组件状态，避免下一次渲染复用旧 widget。
_TURNSTILE_CLEANUP_SCRIPT = (
    "(name) => { try { const s = window[name];"
    " if (s && s.widget !== undefined) window.turnstile.remove(s.widget); }"
    " catch (e) {} window[name] = null; }"
)

# 模块级单例：延迟创建，避免导入期副作用，也便于停止后重建。
_SINGLETON_LOCK = threading.Lock()
_SOLVER_SERVICE: Optional["CloudflareSolverService"] = None
_BROWSER_SOLVER: Optional["CloudflareChallengeSolver"] = None


def _solver_service() -> "CloudflareSolverService":
    """外部求解服务客户端单例。"""
    global _SOLVER_SERVICE
    with _SINGLETON_LOCK:
        if _SOLVER_SERVICE is None:
            _SOLVER_SERVICE = CloudflareSolverService()
        return _SOLVER_SERVICE


def get_cloudflare_solver() -> "CloudflareChallengeSolver":
    """本地反盾过盾器单例（只持有按需浏览器会话，不占常驻内存）。"""
    global _BROWSER_SOLVER
    with _SINGLETON_LOCK:
        if _BROWSER_SOLVER is None:
            _BROWSER_SOLVER = CloudflareChallengeSolver()
        return _BROWSER_SOLVER


def is_cloudflare_challenge(
        text: str = "", status_code: int = 200, headers: Optional[dict] = None
) -> bool:
    """判断正文/响应头是否命中 Cloudflare 质询（有效 JSON 业务响应不算）。

    自有规则覆盖 ``cf-mitigated`` 头与 Managed Challenge，平台
    ``under_challenge`` 作为补充（DDoS-GUARD 等标题与老选择器）。
    """
    headers = {
        str(key).lower(): str(value).lower() for key, value in (headers or {}).items()
    }
    if headers.get("cf-mitigated") == "challenge":
        return True
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return True
    content_type = headers.get("content-type", "")
    stripped = lowered.strip()
    if "json" in content_type or (stripped.startswith("{") and stripped.endswith("}")):
        return False
    if _platform_under_challenge is not None and any(
            hint in lowered for hint in _CHALLENGE_HINTS
    ):
        try:
            if _platform_under_challenge(str(text or "")):
                return True
        except Exception:
            pass
    return (
            status_code in {403, 503}
            and headers.get("server") == "cloudflare"
            and any(marker in lowered for marker in _BLOCK_MARKERS)
    )


def is_cloudflare_response(response: Any) -> bool:
    """判断 HTTP 响应对象是否命中 Cloudflare 拦截质询页。"""
    if response is None:
        return False
    try:
        status = int(getattr(response, "status_code", 0) or 0)
        headers = getattr(response, "headers", {}) or {}
        if str(headers.get("cf-mitigated") or "").lower() == "challenge":
            return True
        if status not in {403, 503}:
            return False
        if "cloudflare" not in str(headers.get("server") or "").lower():
            return False
        body = ""
        try:
            body = str(getattr(response, "text", "") or "")[:4096]
        except Exception:
            pass
        return is_cloudflare_challenge(body, status, headers)
    except Exception:
        return False


def browser_proxy(proxy: Any) -> Optional[Dict[str, str]]:
    """把请求代理转换成 Playwright/CloakBrowser 需要的代理配置。"""
    proxies = normalize_proxies(proxy) or {}
    address = proxies.get("https") or proxies.get("http")
    if not address:
        return None
    parsed = urlparse(str(address))
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"{parsed.scheme}://{host}"
    if parsed.port:
        server += f":{parsed.port}"
    result = {"server": server}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


def launch_challenge_context(proxy: Any):
    """拉起反盾浏览器上下文：优先平台接口（v3），回退 cloakbrowser（v2）。"""
    options = {
        "headless": True,
        "proxy": browser_proxy(proxy),
        "humanize": platform_setting("CLOAKBROWSER_HUMANIZE", True),
        "human_preset": platform_setting("CLOAKBROWSER_HUMAN_PRESET", "careful"),
    }
    if _platform_launch_context is not None:
        return _platform_launch_context(**options)
    if launch_context is None:
        raise RuntimeError("未安装 cloakbrowser，无法拉起反盾浏览器上下文")
    return launch_context(**options)


def block_heavy_resources(page) -> None:
    """拦截图片、多媒体与字体，保留 Cloudflare 相关请求。"""

    def _route_filter(route):
        try:
            url = str(route.request.url or "").lower()
            if "cloudflare" in url:
                route.continue_()
                return
            if route.request.resource_type in {"image", "media", "font"}:
                route.abort()
                return
        except Exception:
            pass
        try:
            route.continue_()
        except Exception:
            pass

    try:
        page.route("**/*", _route_filter)
    except Exception:
        pass


def click_challenge_frame(page, attempt: int = 0) -> bool:
    """点击可见的 Cloudflare 验证框；``attempt`` 递增时先滚入视口再换点击偏移。

    无显示容器里系统级点击不可用，这里只做页内策略升级；
    轮询方重复调用即可逐级升级，避免同坐标重复空点。
    """
    offset = (30, 42, 54)[min(max(int(attempt or 0), 0), 2)]
    for frame in page.frames:
        if urlsplit(frame.url).hostname != "challenges.cloudflare.com":
            continue
        try:
            element = frame.frame_element()
            if not element.is_visible():
                continue
            box = element.bounding_box()
            if not box or box["width"] < 60 or box["height"] < 30:
                continue
            if attempt:
                try:
                    element.scroll_into_view_if_needed()
                except Exception:
                    pass
            page.mouse.click(box["x"] + offset, box["y"] + box["height"] / 2)
            return True
        except Exception:
            continue
    return False


def turnstile_render_script(state_name: str) -> str:
    """生成渲染并执行 Turnstile 的页面脚本，``state_name`` 为状态变量名。"""
    return _TURNSTILE_RENDER_TEMPLATE.replace(
        "__STATE__", str(state_name or "verification")
    )


def mount_turnstile_page(page, url: str) -> None:
    """在指定地址注入本地 Turnstile 挂载页并等待官方脚本就绪。"""
    page.route(
        url,
        lambda route: route.fulfill(
            status=200, content_type="text/html", body=TURNSTILE_MOUNT_HTML
        ),
    )
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_function(
        "() => typeof window.turnstile?.render === 'function'", timeout=30000
    )


class BrowserPageSession:
    """按需反盾浏览器页：页面流程串行执行，空闲即释放，不后台常驻。

    Playwright 同步对象有线程亲和性，浏览器只在 ``run`` 的会话线程内创建和使用；
    代理变更或流程异常时自动重建会话；空闲超过 ``IDLE_RELEASE_SECONDS``
    由看门狗释放，调用方即使丢失引用（热重载）也不会残留 Chromium 进程。
    """

    def __init__(
            self,
            name: str,
            proxy: Any = None,
            timeout: int = 30,
            prepare: Optional[Any] = None,
            idle_seconds: float = IDLE_RELEASE_SECONDS,
    ) -> None:
        self._name = str(name or "浏览器会话")
        self._proxy = proxy
        self._timeout = max(5, int(timeout or 30))
        self._prepare = prepare
        self._idle_seconds = max(1.0, float(idle_seconds or IDLE_RELEASE_SECONDS))
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"{self._name}-Browser"
        )
        self._context = None
        self._page = None
        self._current_proxy = None
        self._generation = 0
        self._last_used = 0.0
        self._click_attempts = 0
        self._watchdog_stop: Optional[threading.Event] = None
        self._lock = threading.RLock()

    @property
    def timeout_ms(self) -> int:
        return max(5000, self._timeout * 1000)

    @property
    def page_generation(self) -> int:
        """页面重建代数，调用方据此判断本次是否复用了既有会话。"""
        with self._lock:
            return self._generation

    def run(self, callback: Any, proxy: Any = None, wait_timeout: float = 0) -> Any:
        """在会话线程内执行页面流程 ``callback(page)``。"""
        target_proxy = self._proxy if proxy is None else proxy
        budget = float(wait_timeout or 0) or (self._timeout + 60)
        future = self._executor.submit(self._run, callback, target_proxy)
        return future.result(timeout=budget)

    def click_challenge(self) -> bool:
        """在会话线程内点击验证框，并逐次升级点击策略。"""
        page = self._page
        if page is None:
            return False
        attempt = self._click_attempts
        self._click_attempts = attempt + 1
        return click_challenge_frame(page, attempt)

    def close(self) -> None:
        """关闭浏览器上下文（释放动作在会话线程内执行）。"""
        try:
            self._executor.submit(self._close_locked).result(timeout=5)
        except Exception:
            pass

    def _run(self, callback: Any, proxy: Any) -> Any:
        with self._lock:
            self._last_used = time.monotonic()
            self._click_attempts = 0
            page = self._ensure_page(proxy)
            try:
                return callback(page)
            except Exception:
                self._close_locked()
                raise
            finally:
                self._last_used = time.monotonic()

    def _ensure_page(self, proxy: Any):
        if self._context is not None and self._current_proxy != proxy:
            self._close_locked()
        if self._page is not None and not self._page.is_closed():
            return self._page

        self._close_locked()
        self._current_proxy = proxy
        started = time.monotonic()
        self._context = launch_challenge_context(proxy)
        logger.debug(
            f"{self._name}：反盾浏览器已启动（耗时 {time.monotonic() - started:.2f}s）"
        )
        self._page = self._context.new_page()
        try:
            self._page.set_default_timeout(self.timeout_ms)
        except Exception:
            pass
        block_heavy_resources(self._page)
        self._generation += 1
        self._last_used = time.monotonic()
        self._start_watchdog_locked()
        if self._prepare is not None:
            self._prepare(self._page)
        return self._page

    def _start_watchdog_locked(self) -> None:
        """启动空闲看门狗，超时释放浏览器后随之退出。"""
        stop = threading.Event()
        self._watchdog_stop = stop
        interval = min(
            _WATCHDOG_MAX_INTERVAL_SECONDS, max(0.05, self._idle_seconds / 4.0)
        )

        def _watch() -> None:
            while not stop.wait(interval):
                # Playwright 对象有线程亲和性：释放必须回到会话线程执行。
                self._executor.submit(self._release_if_idle)

        threading.Thread(
            target=_watch, name=f"{self._name}-Idle", daemon=True
        ).start()

    def _release_if_idle(self) -> bool:
        """空闲超时则释放浏览器；运行中返回 ``False``，已释放返回 ``True``。"""
        with self._lock:
            if self._context is None:
                return True
            if time.monotonic() - self._last_used < self._idle_seconds:
                return False
            logger.debug(
                f"{self._name}：反盾浏览器空闲 {int(self._idle_seconds)} 秒，已自动释放"
            )
            self._close_locked()
            return True

    def _close_locked(self) -> None:
        stop, self._watchdog_stop = self._watchdog_stop, None
        if stop is not None:
            stop.set()
        context, self._context = self._context, None
        self._page = None
        if context is not None:
            try:
                context.close()
            except Exception as error:
                logger.debug(f"{self._name}：关闭反盾浏览器异常：{error}")


def turnstile_page_token(
        page,
        site_key: str,
        action: str,
        *,
        state_name: str,
        label: str = "",
        deadline: float = 45.0,
        click_challenge: Optional[Any] = None,
) -> str:
    """在已持有的页面上渲染并执行 Turnstile，轮询取回一次性 token。

    供已在会话线程内持有页面的调用方使用，避免嵌套会话执行。
    """
    tag = str(label or "Turnstile")
    page.evaluate(
        turnstile_render_script(state_name),
        {"siteKey": site_key, "action": action},
    )
    clicked = False
    end = time.monotonic() + max(5.0, float(deadline or 45.0))
    while time.monotonic() < end:
        state = page.evaluate(f"() => window.{state_name} || {{}}") or {}
        token = str(state.get("token") or "")
        if token:
            page.evaluate(_TURNSTILE_CLEANUP_SCRIPT, state_name)
            return token
        error = str(state.get("error") or "")
        if error:
            raise RuntimeError(f"{tag} Cloudflare 验证失败：{error}")
        if state.get("interactive") and not clicked and click_challenge is not None:
            clicked = bool(click_challenge())
        page.wait_for_timeout(200)
    raise TimeoutError(f"{tag} Cloudflare 验证超过 {int(deadline)} 秒")


def mint_turnstile_token(
        session: BrowserPageSession,
        site_key: str,
        action: str,
        *,
        state_name: str,
        label: str = "",
        deadline: float = 45.0,
        wait_timeout: float = 0,
) -> str:
    """在按需浏览器会话内渲染并执行 Turnstile，返回一次性 token。"""
    started = time.monotonic()
    generation = session.page_generation
    tag = str(label or "Turnstile")

    def _flow(page) -> str:
        token = turnstile_page_token(
            page,
            site_key,
            action,
            state_name=state_name,
            label=tag,
            deadline=deadline,
            click_challenge=session.click_challenge,
        )
        logger.debug(
            f"{tag} Turnstile 就绪：action={action}，"
            f"复用={generation == session.page_generation}，"
            f"耗时={time.monotonic() - started:.2f}s"
        )
        return token

    return session.run(
        _flow, wait_timeout=wait_timeout or (float(deadline or 45.0) + 30.0)
    )


class CloudflareSolverService:
    """外部求解服务客户端：浏览器跑在服务端，MoviePilot 侧内存占用为 0。

    按端点自动识别形态并缓存：

    * ``turnstile``：Turnstile 求解网关（``/health``；同步 ``POST /solve``，
      兜底 YesCaptcha/CapSolver 风格 ``POST /createTask`` + ``/getTaskResult``）；
    * ``flare``：FlareSolverr / Byparr（``POST /v1``，页面级质询）。
    """

    def __init__(self) -> None:
        self._endpoint = ""
        self._key = ""
        self._flavors: Dict[str, str] = {}
        self._lock = threading.Lock()

    def configure(self, endpoint: Any, key: Any = "") -> None:
        """注入服务地址与令牌；地址留空表示停用，非法地址抛 ``ValueError``。"""
        value = normalize_service_url(endpoint)
        with self._lock:
            if value != self._endpoint:
                self._flavors.clear()
            self._endpoint = value
            self._key = str(key or "").strip()

    @property
    def endpoint(self) -> str:
        """当前生效的服务地址：插件配置优先，未配置时复用平台 FlareSolverr 地址。"""
        with self._lock:
            configured = self._endpoint
        if configured:
            return configured
        try:
            return normalize_service_url(platform_setting("FLARESOLVERR_URL", "") or "")
        except ValueError:
            return ""

    @property
    def key(self) -> str:
        """当前生效的服务令牌。"""
        with self._lock:
            return self._key

    @property
    def enabled(self) -> bool:
        """服务是否已配置。"""
        return bool(self.endpoint)

    def solve_turnstile(
            self, page_url: str, site_key: str, action: str = "", timeout: int = 60
    ) -> Optional[str]:
        """用求解网关铸造 Turnstile token；未配置、形态不符或失败返回 ``None``。"""
        if not page_url or not site_key or not self.enabled:
            return None
        if self._flavor() != "turnstile":
            return None
        budget = max(15, min(int(timeout or 60), _SOLVER_MAX_TIMEOUT))
        started = time.monotonic()
        token = self._solve_sync(
            page_url, site_key, action, budget
        ) or self._solve_task(page_url, site_key, action, budget)
        logger.debug(
            f"Cloudflare: 网关 Turnstile token{'成功' if token else '失败'}"
            f" 耗时 {time.monotonic() - started:.2f}s"
        )
        return token

    def solve_page(
            self, url: str, proxy: Any = None, timeout: int = 60
    ) -> Optional[Dict[str, Any]]:
        """用 FlareSolverr/Byparr 穿透页面级质询，返回 cookies/user_agent/html。"""
        endpoint = self.endpoint
        if not url or not endpoint or self._flavor() != "flare":
            return None

        budget = max(15, min(int(timeout or 60), _SOLVER_MAX_TIMEOUT))
        payload: Dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": budget * 1000,
        }
        if proxy_url := self._proxy_url(proxy):
            payload["proxy"] = {"url": proxy_url}

        host = urlsplit(url).netloc or url
        started = time.monotonic()
        data = self._request("/v1", payload, timeout=budget + 10)
        if not data:
            return None
        status = str((data or {}).get("status") or "").strip().lower()
        solution = (data or {}).get("solution")
        if status != "ok" or not isinstance(solution, dict):
            detail = str((data or {}).get("message") or status or "响应异常")
            logger.debug(f"Cloudflare: 外部过盾失败 [{host}]：{detail[:160]}")
            return None

        cookies = [
            item for item in (solution.get("cookies") or [])
            if isinstance(item, dict) and item.get("name")
        ]
        html = str(solution.get("response") or "")
        if is_cloudflare_challenge(
                html, int(solution.get("status") or 0), solution.get("headers") or {}
        ):
            logger.debug(f"Cloudflare: 外部过盾失败 [{host}]：返回内容仍带质盾")
            return None
        if not cookies and not html:
            logger.debug(f"Cloudflare: 外部过盾失败 [{host}]：未返回可用凭证")
            return None

        logger.debug(
            f"Cloudflare: 外部过盾成功 [{host}]"
            f" 耗时 {time.monotonic() - started:.2f}s（cookies={len(cookies)}）"
        )
        return {
            "cookies": cookies,
            "user_agent": str(solution.get("userAgent") or "").strip(),
            "html": html,
        }

    def _flavor(self) -> str:
        """识别服务形态并缓存：``/health`` 带 solver 字段即求解网关。"""
        endpoint = self.endpoint
        with self._lock:
            cached = self._flavors.get(endpoint)
        if cached is not None:
            return cached
        health = self._request(
            "/health", method="GET", timeout=_SOLVER_PROBE_TIMEOUT, quiet=True
        )
        flavor = "turnstile" if isinstance(health, dict) and "solver" in health else "flare"
        with self._lock:
            self._flavors[endpoint] = flavor
        logger.debug(f"Cloudflare: 外部求解服务形态={flavor} [{endpoint}]")
        return flavor

    def _request(
            self,
            path: str,
            payload: Optional[Any] = None,
            method: str = "POST",
            *,
            headers: Optional[Dict[str, str]] = None,
            timeout: float = 15.0,
            quiet: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """请求外部服务并返回 JSON 对象，任何失败均返回 ``None``（``quiet`` 不打日志）。"""
        url = f"{self.endpoint}{path}"
        try:
            if str(method).upper() == "GET":
                response = requests.get(url, headers=headers or {}, timeout=timeout)
            else:
                response = requests.post(
                    url, json=payload, headers=headers or {}, timeout=timeout
                )
            data = response.json()
        except Exception as error:
            if not quiet:
                logger.debug(
                    f"Cloudflare: 外部求解服务请求失败 [{url}]："
                    f"{request_error_summary(error)}"
                )
            return None
        return data if isinstance(data, dict) else None

    def _solve_sync(
            self, page_url: str, site_key: str, action: str, timeout: int
    ) -> Optional[str]:
        """同步形态：``POST /solve`` 直接返回 token。"""
        headers: Dict[str, str] = {}
        if key := self.key:
            headers["X-API-Key"] = key
        data = self._request(
            "/solve",
            {
                "url": page_url,
                "sitekey": site_key,
                "action": action or None,
                "timeout": timeout,
            },
            headers=headers,
            timeout=timeout + 10,
        )
        if not data:
            return None
        token = str(data.get("token") or "").strip()
        if token:
            return token
        detail = str(
            data.get("message") or data.get("code") or data.get("status") or ""
        ).strip()
        if detail:
            logger.debug(f"Cloudflare: 网关同步接口未返回 token：{detail[:160]}")
        return None

    def _solve_task(
            self, page_url: str, site_key: str, action: str, timeout: int
    ) -> Optional[str]:
        """任务形态：``POST /createTask`` 取 taskId，轮询 ``/getTaskResult`` 取 token。"""
        key = self.key
        task: Dict[str, Any] = {
            "type": _SOLVER_TASK_TYPE,
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        if action:
            task["action"] = action
        created = self._request("/createTask", {"clientKey": key, "task": task})
        if not created:
            return None
        task_id = str(created.get("taskId") or created.get("task_id") or "").strip()
        if not task_id:
            detail = (
                    created.get("errorDescription") or created.get("errorCode")
                    or created.get("message") or ""
            )
            logger.debug(f"Cloudflare: 求解网关创建任务失败：{str(detail)[:160]}")
            return None

        deadline = time.monotonic() + max(15, int(timeout or 60))
        while time.monotonic() < deadline:
            time.sleep(_SOLVER_POLL_INTERVAL)
            result = self._request(
                "/getTaskResult", {"clientKey": key, "taskId": task_id}
            )
            if not result:
                continue
            if str(result.get("status") or "").strip().lower() == "ready":
                return self._task_token(result) or None
            if result.get("errorId"):
                detail = (
                        result.get("errorDescription") or result.get("errorCode") or ""
                )
                logger.debug(f"Cloudflare: 求解网关任务失败：{str(detail)[:160]}")
                return None
        logger.debug("Cloudflare: 求解网关任务等待超时")
        return None

    @staticmethod
    def _task_token(result: Dict[str, Any]) -> str:
        """取出任务结果里的 token（兼容 solution 为对象或字符串）。"""
        solution = result.get("solution")
        if isinstance(solution, dict):
            return str(
                solution.get("token") or solution.get("gRecaptchaResponse") or ""
            ).strip()
        if isinstance(solution, str):
            return solution.strip()
        return ""

    @staticmethod
    def _proxy_url(proxy: Any) -> str:
        """把代理配置转换成外部求解服务需要的 URL 形式（带鉴权信息）。"""
        server = browser_proxy(proxy)
        if not server:
            return ""
        address = str(server.get("server") or "")
        username = str(server.get("username") or "")
        if not username or "://" not in address:
            return address
        scheme, _, host = address.partition("://")
        userinfo = quote(username, safe="")
        password = str(server.get("password") or "")
        if password:
            userinfo += f":{quote(password, safe='')}"
        return f"{scheme}://{userinfo}@{host}"


def configure_cf_solver(endpoint: Any, key: Any = "") -> None:
    """注入外部求解服务地址与令牌（插件配置入口）。"""
    _solver_service().configure(endpoint, key)


def cf_solver_endpoint() -> str:
    """当前生效的外部求解服务地址（含平台 FlareSolverr 回退），未配置时为空串。"""
    return _solver_service().endpoint


def cf_solver_key() -> str:
    """当前生效的外部求解服务令牌。"""
    return _solver_service().key


def is_cf_solver_enabled() -> bool:
    """外部求解服务是否已配置。"""
    return _solver_service().enabled


def solve_turnstile_via_api(
        page_url: str, site_key: str, action: str = "", timeout: int = 60
) -> Optional[str]:
    """用外部求解网关铸造 Turnstile token；未配置或失败返回 ``None``。"""
    return _solver_service().solve_turnstile(page_url, site_key, action, timeout)


def solve_via_external_solver(
        url: str, proxy: Any = None, timeout: int = 60
) -> Optional[Dict[str, Any]]:
    """用 FlareSolverr/Byparr 穿透页面级质询，返回 cookies/user_agent/html。"""
    return _solver_service().solve_page(url, proxy, timeout)


def playwright_snapshot(url: str, proxy: Any, timeout: int, gate):
    """用平台浏览器取页面内容与同一会话的 Cookie/UA。"""

    def snapshot(page):
        return {
            "text": page.content() or "",
            "cookies": page.context.cookies(),
            "user_agent": page.evaluate("navigator.userAgent") or "",
        }

    return gate.run(lambda: PlaywrightHelper().action(
        url=url,
        callback=snapshot,
        proxies=browser_proxy(proxy),
        headless=True,
        timeout=timeout,
    ))


class CloudflareChallengeSolver:
    """在按需轻量浏览器里穿透质询盾并提取凭证与页面。"""

    def __init__(self):
        self._session = BrowserPageSession("CF-Challenge", timeout=35)

    def solve(
            self, url: str, proxy: Any = None, timeout: int = 35
    ) -> Optional[Dict[str, Any]]:
        """过盾并提取 cookies/user_agent/html；无可用浏览器或失败返回 ``None``。"""
        if _platform_launch_context is None and launch_context is None:
            return None
        host = urlsplit(url).netloc or url
        started = time.monotonic()
        result = self._solve(url, proxy, timeout)
        passed = bool(result and result.get("cookies"))
        logger.debug(
            f"Cloudflare: 浏览器过盾{'成功' if passed else '失败'}"
            f" [{host}] 耗时 {time.monotonic() - started:.2f}s"
        )
        return result

    def close(self) -> None:
        """关闭内部浏览器会话。"""
        self._session.close()

    def _solve(self, url: str, proxy: Any, timeout: int) -> Optional[Dict[str, Any]]:
        budget = max(15, int(timeout or 35))
        started = time.monotonic()
        deadline = started + budget

        def _flow(page) -> Dict[str, Any]:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            reloads = 0
            while time.monotonic() < deadline:
                if self._challenge_passed(page):
                    return {
                        "cookies": page.context.cookies(),
                        "user_agent": self._user_agent(page),
                        "html": self._page_content(page),
                    }
                if (
                        not self._session.click_challenge()
                        and reloads < _MAX_CHALLENGE_RELOADS
                        and time.monotonic() - started > 10
                ):
                    # 页面找不到验证框且长时间无进展：重载一次刷新挑战窗口。
                    reloads += 1
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                page.wait_for_timeout(400)
            raise TimeoutError("Cloudflare 质询未通过")

        try:
            return self._session.run(_flow, proxy=proxy, wait_timeout=budget + 20)
        except Exception:
            return None

    @staticmethod
    def _challenge_passed(page) -> bool:
        """已拿到 cf_clearance 且标题不再是质询页。"""
        try:
            title = page.title()
        except Exception:
            return False
        lowered = title.lower()
        if "just a moment" in lowered or "attention required" in lowered:
            return False
        if not title.strip():
            return False
        return any(c.get("name") == "cf_clearance" for c in page.context.cookies())

    @staticmethod
    def _user_agent(page) -> str:
        for _ in range(5):
            try:
                value = str(page.evaluate("navigator.userAgent") or "").strip()
            except Exception:
                value = ""
            if value:
                return value
            page.wait_for_timeout(200)
        return ""

    @staticmethod
    def _page_content(page) -> str:
        try:
            return page.content() or ""
        except Exception:
            return ""


def solve_cloudflare_challenge(
        url: str, proxy: Any = None, timeout: int = 35
) -> Optional[Dict[str, Any]]:
    """穿透质盾并提取 cookies/user_agent/html：外部求解服务优先，本地浏览器兜底。"""
    data = solve_via_external_solver(url, proxy=proxy, timeout=timeout)
    if data:
        return data
    return get_cloudflare_solver().solve(url, proxy=proxy, timeout=timeout)


def close_cloudflare_solver() -> None:
    """释放过盾浏览器（插件停止时调用，下次使用会自动重建）。"""
    global _BROWSER_SOLVER
    with _SINGLETON_LOCK:
        solver, _BROWSER_SOLVER = _BROWSER_SOLVER, None
    if solver is None:
        return
    try:
        solver.close()
        logger.debug("已关闭反盾浏览器会话")
    except Exception as error:
        logger.warning(f"关闭反盾浏览器会话异常：{error}")


def _inject_session_cookies(session: Any, cookies: Any, default_domain: str) -> None:
    """把求解得到的 Cookie 写入请求会话（同时兼容带点与不带点的域名）。"""
    for item in cookies or []:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name:
            continue
        raw_domain = str(item.get("domain") or default_domain).strip()
        domain = (
            raw_domain
            if raw_domain.startswith(".") or raw_domain == "localhost"
            else f".{raw_domain}"
        )
        path = str(item.get("path") or "/")
        secure = bool(item.get("secure", True))
        targets = (domain,)
        if domain.startswith(".") and len(domain) > 1:
            targets = (domain, domain.lstrip("."))
        for target in targets:
            try:
                session.cookies.set(
                    name, value, domain=target, path=path, secure=secure
                )
            except Exception:
                pass


def bypass_cloudflare_session(
        session: Any, url: str, proxy: Any = None, timeout: int = 35
) -> bool:
    """穿透质盾并把 cf_clearance 与匹配的 User-Agent 注入请求会话。"""
    if session is None or not url:
        return False
    data = solve_cloudflare_challenge(url, proxy=proxy, timeout=timeout)
    if not data:
        return False

    user_agent = str(data.get("user_agent") or "").strip()
    if user_agent and hasattr(session, "headers"):
        session.headers["user-agent"] = user_agent
        session.headers["User-Agent"] = user_agent

    hostname = urlsplit(url).hostname
    _inject_session_cookies(
        session, data.get("cookies"), f".{hostname}" if hostname else ""
    )
    return True


def fetch_cloudflare_html(url: str, proxy: Any = None, timeout: int = 35) -> str:
    """穿透质盾并返回渲染后的 HTML 内容。"""
    data = solve_cloudflare_challenge(url, proxy=proxy, timeout=timeout)
    if not data or not data.get("html"):
        raise TimeoutError("Cloudflare 验证等待超时")
    return str(data["html"])
