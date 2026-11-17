"""蜗牛搜索与签到客户端。"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from app.log import logger
from bs4 import BeautifulSoup

from ...utils.http_client import (
    RequestGate,
    gated_idempotent_request,
    gated_request,
    normalize_proxies,
    request_error_summary,
    requests,
)


class WoniuError(RuntimeError):
    """蜗牛登录、搜索或签到异常。"""

    def __init__(self, message: str, code: str = "woniu_error"):
        super().__init__(message)
        self.code = code


class WoniuClient:
    """蜗牛 Web 客户端，负责维护登录会话、账户信息、每日签到与影视检索。"""

    BASE_URL = "https://www.wn4k.com"
    _SESSION_DATA_KEY = "woniu_auth_session"
    _LOGIN_LOCK = threading.RLock()

    # 站点强制搜索间隔 ≥3 秒，否则返回“跳转提示”中间页
    _SEARCH_MIN_INTERVAL = 3.0
    _INTERSTITIAL_MARKERS = ("跳转提示", "请不要频繁操作，搜索时间间隔")
    _INTERSTITIAL_RETRY_WAIT = 1.0
    _INTERSTITIAL_MAX_ATTEMPTS = 2

    _HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
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
        self._request_timeout = max(5, min(int(request_timeout or 30), 120))
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func
        self._lock = threading.RLock()
        self._session = self._create_session()
        configured_interval = max(1.0, float(request_interval or 1.0))
        self._request_gate = RequestGate.shared(
            "蜗牛",
            f"{self.base_url}|{self.username.casefold()}|{self._proxies}",
            request_interval=configured_interval,
            minimum_interval=1.0,
        )
        # 搜索端点单独限速：站点要求 ≥3 秒
        self._search_gate = RequestGate.shared(
            "蜗牛搜索",
            f"{self.base_url}|{self.username.casefold()}|search",
            request_interval=max(self._SEARCH_MIN_INTERVAL, configured_interval),
            minimum_interval=self._SEARCH_MIN_INTERVAL,
        )
        self._is_banned = False
        self._restore_session()

    @property
    def is_banned(self) -> bool:
        return bool(getattr(self, "_is_banned", False))

    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    @property
    def _timeout(self) -> tuple[int, int]:
        return min(15, self._request_timeout), self._request_timeout

    def _create_session(self):
        session = requests.Session(impersonate="chrome")
        session.headers.update(self._HEADERS)
        return session

    def _restore_session(self) -> None:
        if not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("username") or "").strip() != self.username
            ):
                return
            cookies = data.get("cookies")
            if isinstance(cookies, dict) and cookies:
                for name, value in cookies.items():
                    self._session.cookies.set(name, str(value))
                logger.debug("蜗牛已恢复本地登录会话")
        except Exception as error:
            logger.debug(f"蜗牛恢复本地会话失败：{error}")

    def _save_session(self) -> None:
        if not self._save_data_func:
            return
        try:
            cookies = self._session.cookies.get_dict()
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "username": self.username,
                    "cookies": cookies,
                    "updated_at": time.time(),
                } if cookies else {},
            )
        except Exception as error:
            logger.debug(f"蜗牛持久化会话失败：{error}")

    def _session_request(self, *args, **kwargs):
        if self._proxies:
            kwargs.setdefault("proxies", self._proxies)
        return self._session.request(*args, **kwargs)

    def _request(
            self,
            method: str,
            path: str,
            params: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None,
            data: Optional[Any] = None,
            json_data: Optional[Any] = None,
            idempotent: bool = False,
            timeout: Optional[tuple[int, int]] = None,
            gate: Optional[RequestGate] = None,
    ) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        req_headers = dict(self._HEADERS)
        if headers:
            req_headers.update(headers)
        req_timeout = timeout or self._timeout
        gate_func = gated_idempotent_request if idempotent else gated_request

        try:
            return gate_func(
                gate or self._request_gate,
                self._session_request,
                method,
                url,
                params=params,
                headers=req_headers,
                data=data,
                json=json_data,
                timeout=req_timeout,
            )
        except requests.exceptions.HTTPError as error:
            status_code = getattr(getattr(error, "response", None), "status_code", 0)
            if status_code in (401, 403):
                raise WoniuError(f"蜗牛身份验证失败 (HTTP {status_code})", "woniu_auth_failed") from error
            raise WoniuError(f"蜗牛请求失败: {request_error_summary(error)}", "woniu_http_error") from error
        except Exception as error:
            raise WoniuError(f"蜗牛网络异常: {request_error_summary(error)}", "woniu_network_error") from error

    def _login(self, force: bool = False) -> None:
        if not self.is_configured:
            raise WoniuError("蜗牛账号或密码未配置", "woniu_not_configured")

        with self._LOGIN_LOCK:
            if not force and self._has_logged_in_cookies():
                return

            logger.info("正在登录蜗牛账号...")
            resp = self._request(
                "POST",
                "/user/login.html",
                data={
                    "user_name": self.username,
                    "user_pwd": self.password,
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{self.base_url}/user/login.html",
                },
            )
            try:
                payload = resp.json()
            except Exception as error:
                raise WoniuError("蜗牛登录响应解析失败", "woniu_login_parse_error") from error

            if payload.get("code") != 1:
                message = str(payload.get("msg") or "登录失败")
                if any(w in message for w in ("封禁", "禁用", "锁定", "banned", "blocked", "冻结")):
                    self._is_banned = True
                    raise WoniuError(f"蜗牛账号被封禁: {message}", "woniu_account_banned")
                raise WoniuError(f"蜗牛登录失败: {message}", "woniu_login_failed")
            self._save_session()
            logger.info("蜗牛账号登录成功")

    def _has_logged_in_cookies(self) -> bool:
        cookies = self._session.cookies.get_dict()
        return bool(cookies.get("user_id") and cookies.get("user_check"))

    def check_login(self, force: bool = False) -> bool:
        """确保客户端当前已处于有效登录状态。"""
        if getattr(self, "_is_banned", False):
            raise WoniuError("蜗牛账号已被封禁，已自动禁用搜索", "woniu_account_banned")
        if not self.is_configured:
            return False

        with self._lock:
            if force or not self._has_logged_in_cookies():
                self._login(force=True)
                return True
            try:
                resp = self._request("GET", "/user/index.html", idempotent=True)
                if "/user/login" in str(resp.url).casefold() or "用户登录" in resp.text:
                    self._login(force=True)
                return True
            except Exception as error:
                logger.debug(f"蜗牛验证登录状态失败，尝试重新登录: {error}")
                self._login(force=True)
                return True

    def get_account_info(self) -> Dict[str, Any]:
        """获取蜗牛会员中心个人资料与签到天数。"""
        self.check_login()
        resp = self._request("GET", "/user/index.html", idempotent=True)
        if any(w in resp.text for w in
               ("账号已封禁", "您的账号已被封禁", "账号被封禁", "已被管理员禁用", "账号异常锁定")):
            self._is_banned = True
            raise WoniuError("蜗牛账号已被封禁", "woniu_account_banned")
        soup = BeautifulSoup(resp.text, "html.parser")
        info_text = soup.get_text(separator="\n", strip=True)
        name_match = re.search(r"用户名\s*([^\n]+)", info_text)
        level_match = re.search(r"会员组\s*([^\n]+)", info_text)
        points_match = re.search(r"积分\s*(\d+)", info_text)
        reg_match = re.search(r"注册时间\s*([\d\-:\s]+)", info_text)

        name = name_match.group(1).strip() if name_match else self.username
        level = level_match.group(1).strip() if level_match else "普通会员"
        points = int(points_match.group(1)) if points_match else 0
        reg_time = reg_match.group(1).strip() if reg_match else ""

        # 从签到页提取累计签到天数
        signin_days: Optional[int] = None
        try:
            chk_resp = self._request("GET", "/user/checkin.html", idempotent=True)
            chk_soup = BeautifulSoup(chk_resp.text, "html.parser")
            chk_text = chk_soup.get_text(separator=" ", strip=True)
            days_match = re.search(r"连续签到\s*(\d+)\s*天", chk_text)
            total_match = re.search(r"累计签到\s*(\d+)\s*次", chk_text)
            if total_match:
                signin_days = int(total_match.group(1))
            elif days_match:
                signin_days = int(days_match.group(1))
        except Exception as error:
            logger.debug(f"蜗牛读取签到天数失败：{error}")

        return {
            "name": name,
            "level": level,
            "points": points,
            "signin_days": signin_days,
            "registered_at": reg_time,
            "details": {},
        }

    def checkin(self, mode: str = "normal") -> Dict[str, Any]:
        """执行每日签到打卡并返回标准化契约数据。"""
        self.check_login()
        before_profile = self.get_account_info()

        resp = self._request(
            "POST",
            "/user/checkin.html",
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/user/checkin.html",
            },
        )
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/user/checkin.html",
        }
        try:
            payload = resp.json()
        except Exception as error:
            raise WoniuError("蜗牛签到响应解析失败", "woniu_checkin_parse_error") from error

        if isinstance(payload, str):
            if "用户登录" in payload or "login" in payload:
                self._login(force=True)
                resp = self._request("POST", "/user/checkin.html", headers=headers)
                try:
                    payload = resp.json()
                except Exception as error:
                    raise WoniuError("蜗牛签到重试响应解析失败", "woniu_checkin_parse_error") from error

        if not isinstance(payload, dict):
            raise WoniuError(f"蜗牛签到响应数据异常：{str(payload)[:60]}", "woniu_checkin_invalid")

        code = payload.get("code")
        raw_msg = str(payload.get("msg") or "").strip()
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}

        already_checked_in = (code == 1001 or "今日已签到" in raw_msg or "已经签到" in raw_msg)
        success = bool(code == 1 or already_checked_in)

        if not success:
            raise WoniuError(f"蜗牛签到失败：{raw_msg or '未知错误'}", "woniu_checkin_failed")

        status_text = "今日已签到" if already_checked_in else "签到成功"
        points_change = int(info.get("points") or 10) if not already_checked_in else 0

        after_profile = self.get_account_info()
        serial_days = info.get("serial_days")
        try:
            signin_days = int(serial_days) if serial_days is not None else after_profile.get("signin_days")
        except (TypeError, ValueError):
            signin_days = after_profile.get("signin_days")

        msg_parts = [status_text]
        if points_change > 0:
            msg_parts.append(f"获得 {points_change} 积分")
        if raw_msg and raw_msg not in (status_text, "success"):
            msg_parts.append(raw_msg)

        message = "，".join(dict.fromkeys(msg_parts))
        return {
            "success": True,
            "already_checked_in": already_checked_in,
            "status": status_text,
            "message": message,
            "mode": "normal",
            "signin_days": signin_days,
            "points_before": int(before_profile.get("points") or 0),
            "points_after": int(after_profile.get("points") or 0),
            "points_change": points_change,
            "details": {
                "status": status_text,
                "signin_days": signin_days,
                "vip_level": after_profile.get("level", ""),
            },
        }

    @classmethod
    def _interstitial_reason(cls, text: str) -> str:
        """返回命中的站点反爬中间页标记，未命中返回空串。"""
        content = str(text or "")
        for marker in cls._INTERSTITIAL_MARKERS:
            if marker in content:
                return marker
        return ""

    def _fetch_soup(self, path: str, gate: Optional[RequestGate] = None):
        """请求并解析页面；命中站点频控中间页时按站点要求等待后重试一次。"""
        last_reason = ""
        for attempt in range(self._INTERSTITIAL_MAX_ATTEMPTS):
            resp = self._request("GET", path, idempotent=True, gate=gate)
            reason = self._interstitial_reason(resp.text)
            if not reason:
                return BeautifulSoup(resp.text, "html.parser")
            last_reason = reason
            if attempt + 1 < self._INTERSTITIAL_MAX_ATTEMPTS:
                logger.debug(
                    f"蜗牛命中站点频控（{reason}），"
                    f"{self._INTERSTITIAL_RETRY_WAIT:.1f} 秒后重试：{path}"
                )
                time.sleep(self._INTERSTITIAL_RETRY_WAIT)
        raise WoniuError(
            f"蜗牛触发搜索频控，降速后仍被拦截：{last_reason}",
            "woniu_rate_limited",
        )

    def search_vods(self, keyword: str, page: int = 1) -> List[Dict[str, Any]]:
        """检索影片列表。"""
        if not keyword:
            return []
        encoded_kw = quote(keyword)
        if page > 1:
            path = f"/vodsearch/-------------/page/{page}/?wd={encoded_kw}"
        else:
            path = f"/vodsearch/-------------/?wd={encoded_kw}"

        soup = self._fetch_soup(path, gate=self._search_gate)
        cards = soup.find_all(class_="video-card")
        results: List[Dict[str, Any]] = []

        for card in cards:
            href = card.get("href") or ""
            vid_match = re.search(r"/voddetail/(\d+)/", href)
            if not vid_match:
                continue
            vod_id = vid_match.group(1)
            title = card.get("title")
            if not title:
                title_elem = card.find(class_="video-title")
                title = title_elem.get_text(strip=True) if title_elem else ""

            meta_elem = card.find(class_="video-meta")
            meta = meta_elem.get_text(strip=True) if meta_elem else ""

            score_elem = card.find(class_="video-score")
            score = score_elem.get_text(strip=True) if score_elem else ""

            episode_elem = card.find(class_="video-episode")
            episode = episode_elem.get_text(strip=True) if episode_elem else ""

            img_elem = card.find("img")
            poster = img_elem.get("src") if img_elem else ""

            results.append({
                "id": vod_id,
                "title": title or f"影片 #{vod_id}",
                "meta": meta,
                "score": score,
                "episode": episode,
                "poster": poster,
                "detail_url": f"{self.base_url}/voddetail/{vod_id}/",
            })

        return results

    def get_vod_detail(self, vod_id: str) -> List[Dict[str, Any]]:
        """读取指定影片详情页，获取解锁后的直链资源。"""
        # 必须确保登录，否则详情页直链会被屏蔽显示为“登录后可见”
        self.check_login()
        soup = self._fetch_soup(f"/voddetail/{vod_id}/")
        items = soup.find_all(class_="pan-link-item")
        resources: List[Dict[str, Any]] = []

        page_title = soup.title.string.split(" - ")[0] if soup.title else ""

        for idx, item in enumerate(items):
            title_elem = item.find(class_="pan-link-title")
            meta_elem = item.find(class_="pan-link-meta")
            btn_elem = item.find("a", class_="pan-link-btn")

            title = title_elem.get_text(strip=True) if title_elem else page_title
            raw_meta = meta_elem.get_text(strip=True) if meta_elem else ""
            link_url = btn_elem.get("href") if btn_elem else raw_meta

            if not link_url or "登录后可见" in link_url or "已锁定" in link_url:
                continue

            resources.append({
                "id": f"{vod_id}:{idx}",
                "title": title,
                "url": str(link_url).strip(),
                "raw_meta": raw_meta,
            })

        return resources

    def clear_cache(self) -> Dict[str, int]:
        return {"session": int(self._has_logged_in_cookies())}

    def close(self) -> None:
        with self._lock:
            try:
                self._session.close()
            except Exception:
                pass
