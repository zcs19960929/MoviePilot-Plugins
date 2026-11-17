"""移动云盘（139 云盘）客户端：签名请求、令牌刷新与扫码登录。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import requests
from Crypto.Cipher import AES
from app.log import logger
from app.utils.string import StringUtils

from ..common import DriveRateLimiter


class Yun139ApiError(RuntimeError):
    """移动云盘请求或协议错误。"""

    def __init__(self, message: str, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = str(code or "")
        self.status_code = int(status_code or 0)


@dataclass(frozen=True)
class Yun139AuthInfo:
    """Authorization 中解析出的账号、令牌与过期时间。"""

    authorization: str
    prefix: str
    account: str
    token: str
    expires_at: float = 0.0


class Yun139Client:
    """移动云盘个人云客户端。"""

    WEB_ORIGIN = "https://yun.139.com"
    ROUTE_URL = "https://user-njs.yun.139.com/user/route/qryRoutePolicy"
    QUOTA_URL = "https://user-njs.yun.139.com/user/disk/quota/detail"
    REFRESH_URL = "https://aas.caiyun.feixin.10086.cn/tellin/authTokenRefresh.do"
    QR_LOGIN_URL = "https://user-njs.yun.139.com/user/thirdlogin"
    QR_PAGE = "https://yun.139.com/w/#/qrcLogin"

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    CLIENT_VERSION = "7.14.0"
    DEVICE_INFO = "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||"
    CLIENT_INFO = "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||"
    REFRESH_ADVANCE_SECONDS = 10 * 3600
    AUTH_ERROR_CODES = ("9000", "9008", "9100", "100002")
    QR_TIMEOUT_SECONDS = 300
    #: 网页扫码通道的 AES-256-CBC 传输密钥与内层 AES-128-ECB 密钥。
    QR_TRANSPORT_KEY = b"UqEZkrjCKfa02pP6jntzFmkzOz86zHUC"
    QR_DATA_KEY = b"qPqDw263XgFgL3u8"
    QR_CLIENT_TYPE = 670
    QR_CPID = 292
    QR_PIN_TYPE = 21
    QR_APP_VERSION = "mCloud_4.3.0_536"
    QR_WEB_VERSION = "7.17.9"
    #: 扫码轮询业务码：等待、过期、取消与失败。
    QR_EXPIRED_CODES = {"200059542"}
    QR_CANCELLED_CODES = {"200059549"}
    QR_FAILED_CODES = {
        "200059543", "200059545", "200059546", "200059547",
        "9101", "01000001",
    }

    def __init__(
            self,
            authorization: str = "",
            timeout: float = 60,
            on_token_refresh: Optional[Callable[[str], None]] = None,
    ):
        self.timeout = max(10, min(int(timeout or 60), 300))
        self.on_token_refresh = on_token_refresh
        self._session = requests.Session()
        self._personal_host = ""
        info = self.parse_authorization(authorization)
        self._auth: Optional[Yun139AuthInfo] = info
        self.rate_limiter = DriveRateLimiter.shared(
            "yun139",
            info.account if info else authorization,
            min_interval=0.3,
        )

    @staticmethod
    def parse_authorization(value: str) -> Optional[Yun139AuthInfo]:
        """解析 Authorization；仅当能识别出账号与令牌时才返回凭据。"""
        raw = str(value or "").strip()
        if raw.lower().startswith("basic "):
            raw = raw[6:].strip()
        if not raw:
            return None
        try:
            decoded = base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            return None
        parts = decoded.split(":", 2)
        if len(parts) != 3 or not parts[1].strip() or not parts[2].strip():
            return None
        token = parts[2].strip()
        expires_at = 0.0
        segments = token.split("|")
        if len(segments) >= 4:
            try:
                millis = int(segments[3])
                if millis > 0:
                    expires_at = millis / 1000
            except (TypeError, ValueError):
                expires_at = 0.0
        return Yun139AuthInfo(
            authorization=raw,
            prefix=parts[0].strip() or "pc",
            account=parts[1].strip(),
            token=token,
            expires_at=expires_at,
        )

    @property
    def authorization(self) -> str:
        return self._auth.authorization if self._auth else ""

    @property
    def account(self) -> str:
        return self._auth.account if self._auth else ""

    @property
    def expires_at(self) -> float:
        return self._auth.expires_at if self._auth else 0.0

    def is_configured(self) -> bool:
        return bool(self._auth)

    def check_login(self) -> bool:
        """校验当前令牌可用：能解析出账号且个人云路由可访问。"""
        if not self._auth:
            return False
        try:
            return bool(self.personal_host())
        except Exception as error:
            logger.debug(f"移动云盘登录校验失败：{error}")
            return False

    def close(self) -> None:
        self._session.close()

    def refresh(self) -> str:
        """刷新过期令牌并回写插件配置。"""
        info = self._auth
        if info is None:
            raise Yun139ApiError("移动云盘 Authorization 无效，请重新抓取或扫码登录")
        body = (
                "<root><token>" + self._xml_escape(info.token)
                + "</token><account>" + self._xml_escape(info.account)
                + "</account><clienttype>656</clienttype></root>"
        )
        response = self.rate_limiter.call(
            self._session.post,
            self.REFRESH_URL,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/xml;charset=UTF-8",
                "Referer": self.WEB_ORIGIN + "/",
                "User-Agent": self.USER_AGENT,
            },
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        if response.status_code != 200:
            raise Yun139ApiError(
                f"移动云盘令牌刷新失败（HTTP {response.status_code}）",
                status_code=response.status_code,
            )
        fields = self._xml_values(response.text)
        if str(fields.get("return") or "").strip() != "0":
            raise Yun139ApiError(
                str(fields.get("desc") or "").strip() or "移动云盘令牌刷新失败"
            )
        token = str(fields.get("token") or fields.get("accessToken") or "").strip()
        if not token:
            raise Yun139ApiError("移动云盘刷新成功但未返回新令牌")
        new_value = base64.b64encode(
            f"{info.prefix}:{info.account}:{token}".encode("utf-8")
        ).decode("ascii")
        self._auth = self.parse_authorization(new_value) or Yun139AuthInfo(
            authorization=new_value,
            prefix=info.prefix,
            account=info.account,
            token=token,
        )
        if self.on_token_refresh:
            self.on_token_refresh(self._auth.authorization)
        return self._auth.authorization

    def ensure_authorization(self) -> str:
        """首次业务请求前按需刷新即将过期的令牌。"""
        info = self._auth
        if info is None:
            raise Yun139ApiError("请先配置移动云盘 Authorization 或扫码登录")
        if not info.expires_at:
            return info.authorization
        if info.expires_at - time.time() >= self.REFRESH_ADVANCE_SECONDS:
            return info.authorization
        try:
            return self.refresh()
        except Exception as error:
            logger.warning(f"移动云盘令牌预刷新失败，继续使用原令牌：{error}")
            return info.authorization

    @staticmethod
    def _xml_escape(value: str) -> str:
        return (
            str(value or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    @staticmethod
    def _xml_values(text: str) -> Dict[str, str]:
        """从刷新接口的 XML 响应中取出扁平字段，避免引入 XML 解析依赖。"""
        return {
            name: value
            for name, value in re.findall(
                r"<([A-Za-z0-9_]+)>\s*([^<]*?)\s*</\1>", str(text or "")
            )
        }

    @staticmethod
    def _encode_uri_component(value: str) -> str:
        """与 JS encodeURIComponent 对齐，保留 !'()* 与 ~ 不转义。"""
        return quote(str(value or ""), safe="-_.!~*'()")

    @classmethod
    def calc_sign(cls, body: str, timestamp: str, rand: str) -> str:
        """按官方规则计算 mcloud-sign。"""
        encoded = cls._encode_uri_component(body)
        sorted_body = "".join(sorted(encoded))
        first = hashlib.md5(
            base64.b64encode(sorted_body.encode("utf-8"))
        ).hexdigest()
        second = hashlib.md5(f"{timestamp}:{rand}".encode("utf-8")).hexdigest()
        return hashlib.md5((first + second).encode("utf-8")).hexdigest().upper()

    @staticmethod
    def _random_string(length: int = 16) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def _build_headers(
            self,
            body_text: str,
            *,
            with_auth: bool = True,
            device_info: str = "",
            client_info: str = "",
            version: str = "",
    ) -> Dict[str, str]:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        rand = self._random_string(16)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "CMS-DEVICE": "default",
            "Content-Type": "application/json;charset=UTF-8",
            "Inner-Hcy-Router-Https": "1",
            "Caller": "web",
            "mcloud-channel": "1000101",
            "mcloud-client": "10701",
            "mcloud-route": "001",
            "mcloud-sign": f"{timestamp},{rand},{self.calc_sign(body_text, timestamp, rand)}",
            "mcloud-version": version or self.CLIENT_VERSION,
            "Origin": self.WEB_ORIGIN,
            "Referer": self.WEB_ORIGIN + "/w/",
            "User-Agent": self.USER_AGENT,
            "x-DeviceInfo": device_info or self.DEVICE_INFO,
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
            "x-yun-api-version": "v1",
            "x-yun-app-channel": "10000034",
            "x-yun-channel-source": "10000034",
            "x-yun-client-info": client_info or self.CLIENT_INFO,
            "x-yun-module-type": "100",
            "x-yun-svc-type": "1",
        }
        if with_auth:
            headers["Authorization"] = f"Basic {self.ensure_authorization()}"
        return headers

    def _post_json(
            self,
            url: str,
            body: Dict[str, Any],
            *,
            payload_override: Optional[str] = None,
            sign_text: str = "",
            headers_override: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """发送一次签名 POST，返回响应信封。"""
        body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        headers = headers_override or self._build_headers(sign_text or body_text)
        response = self.rate_limiter.call(
            self._session.post,
            url,
            data=(payload_override if payload_override is not None else body_text).encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        if response.status_code in (401, 403):
            raise Yun139ApiError(
                "移动云盘认证已过期，请重新抓取 Authorization",
                code=str(response.status_code),
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise Yun139ApiError(
                f"移动云盘接口异常（HTTP {response.status_code}）",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise Yun139ApiError("移动云盘返回了无法解析的响应") from error
        if not isinstance(payload, dict):
            raise Yun139ApiError("移动云盘返回结构异常")
        return payload

    @classmethod
    def _check_envelope(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """校验响应信封并按官方错误码分类抛出。"""
        success = payload.get("success")
        code = str(payload.get("code") or "").strip()
        message = str(payload.get("message") or payload.get("msg") or "").strip()
        if success is False and code not in {"0", "0000", ""}:
            if code in cls.AUTH_ERROR_CODES:
                raise Yun139ApiError(
                    message or "移动云盘认证已过期，请重新抓取 Authorization",
                    code=code,
                )
            if code in {"403", "100403"}:
                raise Yun139ApiError(
                    f"移动云盘权限不足：{message or '无访问权限'}", code=code
                )
            if code == "429":
                raise Yun139ApiError(
                    f"移动云盘接口限流：{message or '请稍后重试'}", code=code
                )
            raise Yun139ApiError(
                f"移动云盘接口返回错误（{code}）：{message or '未知原因'}",
                code=code,
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    def personal_host(self) -> str:
        """解析并缓存个人云业务主机。"""
        if self._personal_host:
            return self._personal_host
        data = self.signed_request(
            self.ROUTE_URL,
            {
                "userInfo": {
                    "userType": 1,
                    "accountType": 1,
                    "accountName": self.account,
                },
                "modAddrType": 1,
            },
        )
        for item in data.get("routePolicyList") or []:
            if str(item.get("modName") or "").strip().lower() != "personal":
                continue
            host = str(item.get("httpsUrl") or item.get("httpUrl") or "").strip()
            if host:
                self._personal_host = host.rstrip("/")
                return self._personal_host
        raise Yun139ApiError("移动云盘路由策略未返回个人云主机")

    def signed_request(
            self, url: str, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """签名请求；认证失效时刷新令牌并重试一次。"""
        request_body = dict(body or {})
        try:
            return self._check_envelope(self._post_json(url, request_body))
        except Yun139ApiError as error:
            if error.code not in self.AUTH_ERROR_CODES and error.status_code != 401:
                raise
        self.refresh()
        return self._check_envelope(self._post_json(url, request_body))

    def request(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用个人云业务接口。"""
        return self.signed_request(self.personal_host() + path, body)

    def post_encrypted(
            self, url: str, payload: str, headers: Dict[str, str]
    ) -> bytes:
        """分享接口的请求体与响应都是密文，这里只负责收发原始字节。"""
        response = self.rate_limiter.call(
            self._session.post,
            url,
            data=payload.encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        if response.status_code >= 400:
            raise Yun139ApiError(
                f"移动云盘分享接口异常（HTTP {response.status_code}）",
                status_code=response.status_code,
            )
        return response.content

    def put_part(self, upload_url: str, chunk: bytes) -> None:
        """分片直传对象存储；该地址自带签名，无需业务请求头。"""
        response = self.rate_limiter.call(
            self._session.put,
            upload_url,
            data=chunk,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": self.WEB_ORIGIN,
                "Referer": self.WEB_ORIGIN + "/",
                "User-Agent": self.USER_AGENT,
            },
            timeout=max(self.timeout, 120),
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        if response.status_code not in (200, 201, 204):
            raise Yun139ApiError(
                f"移动云盘分片上传失败（HTTP {response.status_code}）",
                status_code=response.status_code,
            )

    def get_account_info(self) -> Dict[str, Any]:
        """读取账号与容量信息。"""
        try:
            data = self.signed_request(self.QUOTA_URL, {"userDomainId": self.account})
            _MB = 1024 * 1024
            total_mb = int(data.get("diskSize") or 0)
            free_mb = int(data.get("freeDiskSize") or 0)
            if total_mb <= 0:
                raise Yun139ApiError("移动云盘容量查询返回异常数据")
            free_mb = max(0, min(free_mb, total_mb))
            used_mb = total_mb - free_mb
            return {
                "connected": True,
                "user": {
                    "name": self._masked_account(),
                    "membership_supported": False,
                    "is_vip": False,
                    "is_forever_vip": False,
                    "vip_expire_date": "",
                },
                "storage": {
                    "total": StringUtils.str_filesize(total_mb * _MB),
                    "used": StringUtils.str_filesize(used_mb * _MB),
                    "remaining": StringUtils.str_filesize(free_mb * _MB),
                },
            }
        except Exception as error:
            return {"connected": False, "error": str(error)}

    def _masked_account(self) -> str:
        account = self.account or "移动云盘用户"
        if len(account) >= 7 and account.isdigit():
            return f"{account[:3]}****{account[-4:]}"
        if "@" in account:
            name, _, domain = account.partition("@")
            return f"{name[:2] or name}***@{domain}"
        return account

    @staticmethod
    def _qr_sec_info(session_id: str) -> str:
        return hashlib.sha1(
            f"fetion.com.cn:{session_id}".encode("utf-8")
        ).hexdigest().upper()

    @classmethod
    def _qr_encrypt_transport(cls, body: Dict[str, Any]) -> str:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        pad_len = AES.block_size - len(raw) % AES.block_size
        iv = secrets.token_bytes(AES.block_size)
        cipher = AES.new(cls.QR_TRANSPORT_KEY, AES.MODE_CBC, iv)
        padded = raw + bytes([pad_len]) * pad_len
        return base64.b64encode(iv + cipher.encrypt(padded)).decode("ascii")

    @classmethod
    def _qr_unpad(cls, plain: bytes) -> bytes:
        pad_len = plain[-1] if plain else 0
        if 0 < pad_len <= AES.block_size:
            plain = plain[:-pad_len]
        start, end = plain.find(b"{"), plain.rfind(b"}")
        return plain[start:end + 1] if 0 <= start < end else plain

    @classmethod
    def _qr_decrypt_transport(cls, data: Any) -> bytes:
        if isinstance(data, (bytes, bytearray)):
            text = bytes(data).decode("ascii", "ignore")
        else:
            text = str(data or "")
        text = text.strip().strip('"')
        raw = base64.b64decode(text + "=" * (-len(text) % 4))
        if len(raw) < AES.block_size or len(raw) % AES.block_size:
            raise Yun139ApiError("移动云盘扫码响应密文长度异常")
        iv, cipher_text = raw[:AES.block_size], raw[AES.block_size:]
        plain = AES.new(cls.QR_TRANSPORT_KEY, AES.MODE_CBC, iv).decrypt(cipher_text)
        return cls._qr_unpad(plain)

    @classmethod
    def _qr_decrypt_data_field(cls, value: str) -> bytes:
        """成功响应内层 data 为 hex 密文，用 AES-128-ECB 再解一层。"""
        text = str(value or "").strip()
        try:
            raw = bytes.fromhex(text)
        except ValueError:
            raw = base64.b64decode(text)
        return cls._qr_unpad(AES.new(cls.QR_DATA_KEY, AES.MODE_ECB).decrypt(raw))

    @staticmethod
    def _qr_find_value(root: Any, keys: tuple[str, ...]) -> str:
        """在嵌套结构中按 key 名递归查找首个非空字符串。"""
        wanted = {key.lower() for key in keys}
        if isinstance(root, dict):
            for key, value in root.items():
                if str(key).lower() in wanted and str(value or "").strip():
                    return str(value).strip()
            for value in root.values():
                found = Yun139Client._qr_find_value(value, keys)
                if found:
                    return found
        elif isinstance(root, list):
            for value in root:
                found = Yun139Client._qr_find_value(value, keys)
                if found:
                    return found
        return ""

    def create_qrcode_login(self, client_type: str = "") -> Dict[str, Any]:
        """创建扫码会话，返回待扫码的二维码内容。"""
        session_id = self._random_string(16)
        visitor_id = self._random_string(32)
        return {
            "qr_url": f"{self.QR_PAGE}?sID={session_id}&dID={visitor_id}&cType=9",
            "sid": session_id,
            "vid": visitor_id,
            "created_at": int(time.time()),
            "expires_in": self.QR_TIMEOUT_SECONDS,
            "interval": 3,
        }

    def check_qrcode_login(self, **kwargs: Any) -> Dict[str, Any]:
        """轮询扫码结果，成功时返回可持久化的 Authorization。"""
        session_id = str(kwargs.get("sid") or "").strip()
        visitor_id = str(kwargs.get("vid") or "").strip()
        created_at = int(kwargs.get("created_at") or 0) or int(time.time())
        if not session_id or not visitor_id:
            raise ValueError("缺少移动云盘扫码会话参数")
        if time.time() - created_at > self.QR_TIMEOUT_SECONDS:
            return {"status": "expired", "message": "二维码已过期，请重新获取"}

        plain_body = {
            "msisdn": "",
            "random": "",
            "dycpwd": session_id,
            "cpid": self.QR_CPID,
            "clienttype": self.QR_CLIENT_TYPE,
            "version": self.QR_APP_VERSION,
            "pintype": self.QR_PIN_TYPE,
            "secinfo": self._qr_sec_info(session_id),
            "loginMode": "0",
            "extInfo": {},
        }
        plain_text = json.dumps(plain_body, ensure_ascii=False, separators=(",", ":"))
        headers = self._build_headers(
            plain_text,
            with_auth=False,
            device_info=(
                f"||9|{self.QR_WEB_VERSION}|chrome|120.0.0.0|{visitor_id}"
                "||windows 10||zh-CN|||"
            ),
            client_info=(
                f"||9|{self.QR_WEB_VERSION}|chrome|120.0.0.0|{visitor_id}"
                "||windows 10||zh-CN|||dW5kZWZpbmVk||"
            ),
            version=self.QR_WEB_VERSION,
        )
        headers["hcy-cool-flag"] = "1"
        response = self.rate_limiter.call(
            self._session.post,
            self.QR_LOGIN_URL,
            data=json.dumps(self._qr_encrypt_transport(plain_body)).encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        if response.status_code != 200:
            raise Yun139ApiError(
                f"移动云盘扫码接口异常（HTTP {response.status_code}）",
                status_code=response.status_code,
            )
        try:
            envelope = json.loads(
                self._qr_decrypt_transport(response.content).decode("utf-8")
            )
        except Exception:
            envelope = response.json()
        if not isinstance(envelope, dict):
            raise Yun139ApiError("移动云盘扫码响应无法解析")
        data: Dict[str, Any] = {}
        raw_data = envelope.get("data")
        try:
            if isinstance(raw_data, str) and raw_data.strip():
                data = json.loads(self._qr_decrypt_data_field(raw_data).decode("utf-8"))
            elif isinstance(raw_data, dict):
                data = raw_data
        except Exception:
            data = {}
        source = data or envelope
        account = self._qr_find_value(source, ("account", "msisdn", "phoneNumber"))
        token = self._qr_find_value(source, ("token", "authToken", "accessToken"))
        if account and token:
            authorization = base64.b64encode(
                f"pc:{account}:{token}".encode("utf-8")
            ).decode("ascii")
            self._auth = self.parse_authorization(authorization)
            return {
                "status": "success",
                "message": "登录成功",
                "authorization": authorization,
            }
        code = str(
            envelope.get("code")
            or (data.get("result") or {}).get("resultCode")
            or data.get("resultCode")
            or ""
        ).strip()
        if code in self.QR_EXPIRED_CODES:
            return {"status": "expired", "message": "二维码已过期，请重新获取"}
        if code in self.QR_CANCELLED_CODES:
            return {"status": "cancelled", "message": "已取消登录，请重新扫码"}
        if code in self.QR_FAILED_CODES:
            message = str(
                envelope.get("message")
                or (data.get("result") or {}).get("resultDesc")
                or "扫码登录失败"
            ).strip()
            return {"status": "failed", "message": message}
        if code == "200059548":
            return {"status": "scanned", "message": "已扫码，请在手机上确认登录"}
        return {
            "status": "waiting",
            "message": str(
                envelope.get("message") or "请使用中国移动云盘 App 扫码登录"
            ).strip(),
        }
