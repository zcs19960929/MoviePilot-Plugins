"""聚影普通账号网页资源搜索客户端。"""

import threading
import time
from typing import Any, Callable, Dict, Optional

from app.log import logger

from ...utils.http_client import (
    RequestGate,
    gated_idempotent_request,
    gated_request,
    normalize_proxies,
    request_error_summary,
    requests,
)


class JuyingError(RuntimeError):
    """聚影登录、搜索或资源解析失败。"""

    def __init__(self, message: str, code: str = "juying_error"):
        super().__init__(message)
        self.code = code


class JuyingClient:
    """维护聚影 CSRF、登录令牌和受控 JSON 请求。"""

    BASE_URL = "https://www.jying.top"
    _SESSION_DATA_KEY = "juying_auth_session"
    _LOGIN_LOCK = threading.RLock()

    _HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    def __init__(
            self,
            username: str,
            password: str,
            base_url: str = BASE_URL,
            proxy: Any = None,
            request_timeout: int = 30,
            request_interval: float = 1.0,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
    ):
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self._proxies = normalize_proxies(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 30), 60))
        self._session = self._create_session()
        self._token = ""
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func
        self._lock = threading.RLock()
        self._circuit_open_until = 0.0
        self._user_id_cache = 0
        self._request_gate = RequestGate.shared(
            "聚影",
            f"{self.base_url}|{self.username.casefold()}|{self._proxies}",
            request_interval=request_interval,
            minimum_interval=1,
        )
        self._restore_token()

    @property
    def _timeout(self) -> tuple[int, int]:
        return min(15, self._request_timeout), self._request_timeout

    @classmethod
    def _create_session(cls):
        session = requests.Session(impersonate="chrome")
        session.headers.update(cls._HEADERS)
        return session

    def _session_request(self, *args, **kwargs):
        return self._session.request(*args, **kwargs)

    def _reset_transport(self, error: BaseException, attempt: int) -> None:
        cookies = self._session.cookies.get_dict()
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._create_session()
        for name, value in cookies.items():
            self._session.cookies.set(name, value)
        logger.debug(
            f"聚影连接异常后重建 HTTP 会话："
            f"{type(error).__name__}，重试={attempt}"
        )

    def _restore_token(self) -> None:
        if not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("username") or "").strip() != self.username
            ):
                return
            token = str(data.get("token") or "").strip()
            if token:
                self._token = token
                logger.debug("聚影已恢复持久化登录状态")
        except Exception as error:
            logger.debug(f"聚影恢复持久化登录状态失败：{error}")

    def _save_token(self) -> None:
        if not self._save_data_func:
            return
        try:
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "username": self.username,
                    "token": self._token,
                    "updated_at": int(time.time()),
                } if self._token else {},
            )
        except Exception as error:
            logger.debug(f"聚影持久化登录状态失败：{error}")

    def _set_token(self, token: str = "") -> None:
        self._token = str(token or "").strip()
        self._save_token()

    def _csrf_headers(self) -> Dict[str, str]:
        headers = {
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }
        csrf_token = str(self._session.cookies.get("csrftoken") or "")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        return headers

    @staticmethod
    def _json_response(response: requests.Response) -> bool:
        return "application/json" in str(
            response.headers.get("content-type") or ""
        ).casefold()

    def _ensure_available(self) -> None:
        if not self.username or not self.password:
            raise JuyingError("聚影账号或密码未配置", "juying_not_configured")
        if self._circuit_open_until > time.monotonic():
            raise JuyingError("聚影请求暂时受限，请稍后重试", "juying_rate_limited")

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    def _login(self, force: bool = False) -> None:
        self._ensure_available()
        if self._token and not force:
            return
        with self._LOGIN_LOCK:
            if not force:
                self._restore_token()
                if self._token:
                    return
            try:
                csrf_response = gated_idempotent_request(
                    self._request_gate,
                    self._session_request,
                    "GET",
                    f"{self.base_url}/api/csrf/",
                    on_retry=self._reset_transport,
                    proxies=self._proxies,
                    timeout=self._timeout,
                )
            except requests.exceptions.RequestException as error:
                raise JuyingError(
                    f"聚影登录初始化失败：{request_error_summary(error)}",
                    "juying_login_failed",
                ) from error
            if csrf_response.status_code != 200:
                raise JuyingError(
                    f"聚影 CSRF 初始化失败（HTTP {csrf_response.status_code}）",
                    "juying_login_failed",
                )
            try:
                response = gated_request(
                    self._request_gate,
                    self._session_request,
                    "POST",
                    f"{self.base_url}/api/app/login/",
                    json={"username": self.username, "password": self.password},
                    headers=self._csrf_headers(),
                    proxies=self._proxies,
                    timeout=self._timeout,
                )
            except requests.exceptions.RequestException as error:
                raise JuyingError(
                    f"聚影登录失败：{request_error_summary(error)}",
                    "juying_login_failed",
                ) from error
            if response.status_code != 200 or not self._json_response(response):
                raise JuyingError(
                    f"聚影登录失败（HTTP {response.status_code}）",
                    "juying_login_failed",
                )
            payload = response.json()
            token = (
                str(payload.get("token") or "").strip()
                if isinstance(payload, dict) else ""
            )
            if not token:
                message = payload.get("message") if isinstance(payload, dict) else ""
                raise JuyingError(
                    str(message or "聚影登录未返回会话令牌"),
                    "juying_login_failed",
                )
            self._set_token(token)

    def _request(
            self,
            method: str,
            path: str,
            retry_auth: bool = True,
            protected_access: bool = False,
            **kwargs: Any,
    ) -> Dict[str, Any]:
        self._login()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._csrf_headers())
        headers["X-App-User-Token"] = self._token
        try:
            def request():
                return gated_idempotent_request(
                    self._request_gate,
                    self._session_request,
                    method,
                    f"{self.base_url}{path}",
                    on_retry=self._reset_transport,
                    headers=headers,
                    proxies=self._proxies,
                    timeout=self._timeout,
                    **kwargs,
                )

            response = request()
        except requests.exceptions.RequestException as error:
            raise JuyingError(
                f"聚影请求失败：{request_error_summary(error)}",
                "juying_request_failed",
            ) from error

        refreshed = str(response.headers.get("x-refreshed-token") or "").strip()
        if refreshed:
            self._set_token(refreshed)
        if response.status_code == 401 and retry_auth:
            self._set_token()
            self._login(force=True)
            return self._request(
                method,
                path,
                retry_auth=False,
                protected_access=protected_access,
                **kwargs,
            )
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after") or ""
            try:
                seconds = max(30, min(120, int(float(retry_after))))
            except (TypeError, ValueError):
                seconds = 60
            self._request_gate.activate_cooldown(
                seconds, status=429, reason="聚影 HTTP 429"
            )
            self._circuit_open_until = time.monotonic() + seconds
            raise JuyingError("聚影请求过于频繁，已临时暂停该渠道", "juying_rate_limited")
        if response.status_code >= 400:
            message = ""
            if self._json_response(response):
                try:
                    body = response.json()
                    message = str(body.get("message") or body.get("detail") or "")
                except ValueError:
                    pass
            if response.status_code == 403 and protected_access:
                logger.warning(
                    "聚影资源 access 返回 HTTP 403："
                    f"path={path}，token={'present' if self._token else 'missing'}，"
                    f"csrf={'present' if self._session.cookies.get('csrftoken') else 'missing'}，"
                    f"message={message or '<empty>'}"
                )
                raise JuyingError(
                    message or "聚影资源访问票据已失效",
                    "juying_access_forbidden",
                )
            raise JuyingError(
                message or f"聚影请求失败（HTTP {response.status_code}）",
                "juying_request_failed",
            )
        if not self._json_response(response):
            raise JuyingError(
                "聚影返回了非 JSON 页面，接口响应协议异常或已改版",
                "juying_schema_changed",
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise JuyingError("聚影返回数据格式异常", "juying_schema_changed") from error
        if not isinstance(payload, dict):
            raise JuyingError("聚影返回数据格式异常", "juying_schema_changed")
        return payload

    def request_json(
            self,
            method: str,
            path: str,
            protected_access: bool = False,
            **kwargs,
    ) -> Dict[str, Any]:
        """执行带登录态的聚影 JSON 请求。"""
        with self._lock:
            return self._request(
                method, path, protected_access=protected_access, **kwargs
            )

    def get_account_info(self) -> Dict[str, Any]:
        """读取当前聚影账户及可用积分。"""
        payload = self.request_json("GET", "/api/app/profile/")
        user = payload.get("user") if isinstance(payload, dict) else None
        if (
                payload.get("status") != "success"
                or not isinstance(user, dict)
                or ("points" not in user and "reward_points" not in user)
        ):
            raise JuyingError(
                "聚影账户接口缺少积分字段", "juying_schema_changed"
            )
        raw_points = (
            user.get("points")
            if "points" in user else user.get("reward_points")
        )
        try:
            points = int(raw_points or 0)
        except (TypeError, ValueError) as error:
            raise JuyingError(
                "聚影账户积分格式异常", "juying_schema_changed"
            ) from error
        return {
            "name": str(user.get("username") or user.get("email") or "聚影用户"),
            "email": str(user.get("email") or ""),
            "username": str(user.get("username") or ""),
            "avatar": str(user.get("avatar") or ""),
            "points": max(0, points),
            "level": str(user.get("level_name") or ""),
            "upload_count": max(0, int(user.get("upload_count") or 0)),
            "favorite_count": max(0, int(user.get("favorite_count") or 0)),
            "checkin_days": max(0, int(user.get("checkin_days") or 0)),
            "registered_days": max(0, int(payload.get("registered_days") or 0)),
            "created_at": str(user.get("date_joined") or ""),
        }

    def get_checkin_stats(self) -> Dict[str, Any]:
        """读取聚影当日签到状态与奖励。"""
        payload = self.request_json("GET", "/api/app/checkin/stats/")
        if payload.get("status") != "success":
            raise JuyingError(
                "聚影签到状态接口返回异常", "juying_schema_changed"
            )
        return payload

    def _user_id(self) -> int:
        """取当前账户 id：幸运签到的写操作需要账户校验头。"""
        if self._user_id_cache > 0:
            return self._user_id_cache
        payload = self.request_json("GET", "/api/app/profile/")
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        self._user_id_cache = int(user.get("id") or 0)
        return self._user_id_cache

    def get_luck_stats(self) -> Dict[str, Any]:
        """读取幸运签到状态：今日是否可参与、风险限额与概率表。"""
        payload = self.request_json("GET", "/api/app/checkin/luck/")
        if payload.get("status") != "success":
            raise JuyingError(
                "聚影幸运签到状态接口返回异常", "juying_schema_changed"
            )
        return payload

    def get_bonus_pool(self) -> Dict[str, Any]:
        """读取签到幸运奖池状态：剩余积分与今日可领取情况。"""
        payload = self.request_json("GET", "/api/app/checkin/bonus-pool/")
        if payload.get("status") != "success":
            raise JuyingError(
                "聚影签到奖池接口返回异常", "juying_schema_changed"
            )
        return payload

    def draw_luck(self) -> Dict[str, Any]:
        """执行幸运签到（与普通签到每日二选一，结果不可重抽）。"""
        stats = self.get_luck_stats()
        if stats.get("enabled") is False:
            raise JuyingError("聚影幸运签到未开放", "juying_luck_disabled")
        if stats.get("can_draw") is False:
            raise JuyingError(
                str(stats.get("can_draw_reason") or "聚影幸运签到今日不可参与"),
                "juying_luck_unavailable",
            )
        headers: Dict[str, str] = {}
        user_id = self._user_id()
        if user_id > 0:
            headers["X-Expected-App-User-Id"] = str(user_id)
        return self.request_json(
            "POST",
            "/api/app/checkin/luck/draw/",
            json={"accept_risk": True},
            headers=headers,
        )

    def claim_bonus_pool(self) -> Dict[str, Any]:
        """领取当日签到幸运奖池：免费每日一次，奖池无余额或已领取时跳过。"""
        payload = self.get_bonus_pool()
        pool = payload.get("pool") if isinstance(payload.get("pool"), dict) else {}
        if not pool:
            return {"success": False, "skipped": True, "message": "奖池信息缺失"}
        if pool.get("can_draw") is not True:
            return {
                "success": False,
                "skipped": True,
                "message": str(pool.get("can_draw_reason") or "今日奖池不可领取"),
            }
        pool_id = int(pool.get("id") or 0)
        if pool_id <= 0:
            return {"success": False, "skipped": True, "message": "奖池标识缺失"}
        result = self.request_json(
            "POST", "/api/app/checkin/bonus-pool/draw/", json={"pool_id": pool_id}
        )
        updated = result.get("pool") if isinstance(result.get("pool"), dict) else {}
        my_draw = (
            updated.get("my_draw") if isinstance(updated.get("my_draw"), dict) else {}
        )
        return {
            "success": True,
            "skipped": False,
            "title": str(updated.get("title") or pool.get("title") or ""),
            "award_points": int(my_draw.get("reward_points") or 0),
            "balance_after": my_draw.get("balance_after"),
            "message": str(result.get("message") or "幸运积分已到账"),
        }

    def _signin_normal(self) -> Dict[str, Any]:
        """普通签到：先读状态，未签到再执行。"""
        stats_before = self.get_checkin_stats()
        if bool(stats_before.get("checked_today")):
            return {
                "success": True,
                "already_checked_in": True,
                "status": "今日已签到",
                "message": "今天已经签到",
                "reward_points": stats_before.get("reward_points"),
                "signin_days": stats_before.get("my_total_days"),
            }
        payload: Dict[str, Any] = {}
        try:
            payload = self.request_json("POST", "/api/app/checkin/do/")
        except JuyingError:
            if not self.get_checkin_stats().get("checked_today"):
                raise
        return {
            "success": True,
            "already_checked_in": False,
            "status": "签到成功",
            "message": str(payload.get("message") or "签到成功"),
            "reward_points": stats_before.get("reward_points"),
            "signin_days": self.get_checkin_stats().get("my_total_days"),
        }

    def _signin_luck(self) -> Dict[str, Any]:
        """幸运签到：最高 +50、最多扣 5，与普通签到每日二选一。"""
        stats = self.get_luck_stats()
        draw = stats.get("draw") if isinstance(stats.get("draw"), dict) else {}
        if draw:
            delta = int(draw.get("delta") or 0)
            return {
                "success": True,
                "already_checked_in": True,
                "status": "今日已完成幸运签到",
                "message": f"今日幸运签到结果 {delta:+d} 积分",
                "delta": delta,
                "balance_after": stats.get("user_points"),
            }
        try:
            payload = self.draw_luck()
        except JuyingError as error:
            if error.code == "juying_luck_unavailable":
                # 今日已选择普通签到：记为已签到，不当作失败
                return {
                    "success": True,
                    "already_checked_in": True,
                    "status": "今日已签到",
                    "message": str(error),
                }
            raise
        draw = payload.get("draw") if isinstance(payload.get("draw"), dict) else {}
        delta = int(draw.get("delta") or 0)
        return {
            "success": True,
            "already_checked_in": False,
            "status": "幸运签到成功",
            "message": str(payload.get("message") or f"幸运签到 {delta:+d} 积分"),
            "delta": delta,
            "balance_after": payload.get("user_points"),
        }

    def checkin(self, mode: str = "normal") -> Dict[str, Any]:
        """执行聚影每日签到（普通/幸运二选一），随后领取当日签到幸运奖池。"""
        normalized_mode = str(mode or "normal").strip().lower()
        if normalized_mode not in {"normal", "lucky"}:
            raise JuyingError("聚影签到模式无效", "invalid_mode")
        before = self.get_account_info()
        signin = (
            self._signin_luck()
            if normalized_mode == "lucky"
            else self._signin_normal()
        )
        bonus: Dict[str, Any] = {}
        if signin.get("success"):
            try:
                bonus = self.claim_bonus_pool()
            except JuyingError as error:
                bonus = {"success": False, "skipped": True, "message": str(error)}
        try:
            after = self.get_account_info()
        except JuyingError:
            # 账户接口失败时退回签到/奖池返回的余额，避免误判积分变化
            after = dict(before)
            balance = bonus.get("balance_after") or signin.get("balance_after")
            if balance is not None:
                after["points"] = balance

        points_before = int(before.get("points") or 0)
        points_after = int(after.get("points") or 0)
        if signin.get("already_checked_in"):
            signin_points = 0
        else:
            signin_points = int(
                signin.get("delta", signin.get("reward_points")) or 0
            )

        parts = [
            str(signin.get("status") or ("签到成功" if signin.get("success") else "签到失败"))
        ]
        if bonus.get("success"):
            parts.append(f"签到奖池 +{int(bonus.get('award_points') or 0)} 积分")
        elif not bonus.get("skipped") and bonus.get("message"):
            parts.append(f"奖池未领取：{bonus.get('message')}")
        success = bool(signin.get("success"))
        return {
            "success": success,
            "already_checked_in": bool(signin.get("already_checked_in")),
            "status": str(signin.get("status") or ("签到成功" if success else "签到失败")),
            "message": "；".join(part for part in parts if part),
            "mode": normalized_mode,
            "signin_points": signin_points,
            "points_change": points_after - points_before,
            "points_before": points_before,
            "points_after": points_after,
            "signin_days": int(signin.get("signin_days") or after.get("checkin_days") or 0),
            "status_code": 200,
            "error_code": "",
        }

    def close(self) -> None:
        with self._lock:
            self._session.close()
