"""123 网盘原生客户端实现与调用封装。"""

from __future__ import annotations

import random
import zlib
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import urlparse

import requests

from ..common import DriveRateLimiter, format_size, safe_int

P123_AVAILABLE = True


class P123ApiError(RuntimeError):
    """123网盘 API 请求异常。"""

    def __init__(self, message: str, code: int = 0, raw: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


def check_response(response: Any) -> dict:
    """校验 123 响应是否成功，若失败抛出异常。"""
    if not isinstance(response, dict):
        raise P123ApiError("123网盘返回格式无效")
    code = safe_int(response.get("code"))
    if code not in (0, 200):
        message = str(response.get("message") or response.get("msg") or f"请求错误({code})")
        raise P123ApiError(message, code=code, raw=response)
    return response


def is_success(response: Any) -> bool:
    """快速判定响应是否成功。"""
    if not isinstance(response, dict):
        return False
    return safe_int(response.get("code")) in (0, 200)


def sign_path(path: str, platform: str = "web", version: str = "3") -> Tuple[str, str]:
    """123 官方 CRC32 动态时间戳鉴权签名算法。"""
    table = b"adefghlmyijnopqrstuvbcvwsz"
    random_num = str(round(1e7 * random.random()))
    now_utc = datetime.now(timezone.utc)
    now_cst = now_utc + timedelta(hours=8)
    timestamp = str(int(now_cst.timestamp()))
    now_str = now_cst.strftime("%Y%m%d%H%M").encode("ascii")
    mapped = bytes([table[b - 48] for b in now_str])
    time_sign = str(zlib.crc32(mapped) & 0xFFFFFFFF)
    data = f"{timestamp}|{random_num}|{path}|{platform}|{version}|{time_sign}".encode("utf-8")
    data_sign = str(zlib.crc32(data) & 0xFFFFFFFF)
    return time_sign, f"{timestamp}-{random_num}-{data_sign}"


class P123Client:
    """原生 123 网盘 HTTP 客户端。"""

    BASE_URL = "https://yun.123pan.com/b/api"
    LOGIN_URL = "https://login.123pan.com/api"

    def __init__(self, token: str = "", timeout: int = 30):
        self.token = str(token or "").strip().removeprefix("Bearer ").strip()
        self.timeout = max(5, min(int(timeout or 30), 300))
        self.session = requests.Session()

    def close(self) -> None:
        self.session.close()

    def _headers(self, custom: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "origin": "https://yun.123pan.com",
            "referer": "https://yun.123pan.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "platform": "web",
            "app-version": "3",
        }
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        if custom:
            headers.update(custom)
        return headers

    def request(
            self,
            path: str,
            method: str = "GET",
            *,
            params: Optional[Dict[str, Any]] = None,
            json: Optional[Any] = None,
            data: Optional[Any] = None,
            headers: Optional[Dict[str, str]] = None,
            timeout: Optional[int] = None,
            base_url: Optional[str] = None,
            **kwargs: Any,
    ) -> dict:
        """统一发送 123pan 请求，自动附带官方 CRC32 签名；兼容外部 S3 直传。"""
        kwargs.pop("parse", None)
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            base = base_url or self.BASE_URL
            url = f"{base.rstrip('/')}/{path.lstrip('/')}"

        parsed = urlparse(url)
        req_params = dict(params or {})
        if "123pan.com" in parsed.netloc and method.upper() != "PUT":
            sign_key, sign_val = sign_path(parsed.path, "web", "3")
            req_params[sign_key] = sign_val

        req_headers = self._headers(headers)
        if headers and headers.get("authorization") == "":
            req_headers.pop("authorization", None)

        resp = self.session.request(
            method=method,
            url=url,
            params=req_params,
            json=json,
            data=data,
            headers=req_headers,
            timeout=timeout or self.timeout,
            **kwargs,
        )
        resp.raise_for_status()
        if not resp.content:
            return {"code": 0, "message": "success"}
        try:
            return resp.json()
        except ValueError:
            return {"code": 0, "message": "success", "raw": resp.text}

    def user_info(self) -> dict:
        """获取当前用户信息与容量。"""
        return self.request("user/info", method="GET")

    @classmethod
    def login_qrcode_generate(cls, timeout: int = 30) -> dict:
        """生成扫码登录参数。"""
        session = requests.Session()
        try:
            resp = session.get(
                f"{cls.LOGIN_URL}/user/qr-code/generate",
                headers={
                    "origin": "https://yun.123pan.com",
                    "referer": "https://yun.123pan.com/",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        finally:
            session.close()

    @classmethod
    def login_qrcode_result(cls, uni_id: str, timeout: int = 30) -> dict:
        """轮询扫码状态。"""
        session = requests.Session()
        try:
            resp = session.get(
                f"{cls.LOGIN_URL}/user/qr-code/result",
                params={"uniID": uni_id},
                headers={
                    "origin": "https://yun.123pan.com",
                    "referer": "https://yun.123pan.com/",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        finally:
            session.close()

    def list_files(
            self,
            parent_id: Union[int, str] = 0,
            page: int = 1,
            limit: int = 100,
            order_by: str = "file_id",
            order_direction: str = "desc",
            next_val: Union[int, str] = 0,
    ) -> dict:
        """查询文件列表（保持 driveId 和 parentFileId 驼峰大小写）。"""
        params = {
            "driveId": 0,
            "limit": int(limit),
            "next": int(next_val) if str(next_val).isdigit() else 0,
            "orderBy": str(order_by),
            "orderDirection": str(order_direction),
            "parentFileId": int(parent_id) if str(parent_id).isdigit() else 0,
            "trashed": "false",
            "SearchData": "",
            "Page": int(page),
            "OnlyLookAbnormalFile": "0",
            "event": "homeListFile",
            "operateType": "4",
            "inDirectSpace": "false",
        }
        return self.request("file/list/new", method="GET", params=params)

    def get_download_info(self, payload: Mapping[str, Any]) -> dict:
        """获取下载直链。"""
        body = {
            "driveId": 0,
            "etag": str(payload.get("etag") or payload.get("Etag") or "").strip(),
            "fileId": int(payload.get("file_id") or payload.get("FileID") or payload.get("fileId") or 0),
            "fileName": str(payload.get("file_name") or payload.get("FileName") or payload.get("fileName") or ""),
            "s3keyFlag": str(payload.get("s3_key_flag") or payload.get("S3KeyFlag") or "").strip(),
            "size": int(payload.get("size") or payload.get("Size") or 0),
            "type": int(payload.get("type") or payload.get("Type") or 0),
        }
        return self.request("file/download_info", method="POST", json=body)

    def create_folder(self, name: str, parent_id: Union[int, str] = 0) -> dict:
        """新建目录。"""
        body = {
            "driveId": 0,
            "etag": "",
            "fileName": str(name).strip(),
            "parentFileId": int(parent_id or 0),
            "size": 0,
            "type": 1,
        }
        return self.request("file/upload_request", method="POST", json=body)

    def move(self, file_ids: Union[int, str, List[Union[int, str]]], parent_id: Union[int, str] = 0) -> dict:
        """移动文件或目录。"""
        if isinstance(file_ids, (list, tuple, set)):
            ids = [int(fid) for fid in file_ids if str(fid).isdigit()]
        else:
            ids = [int(file_ids)] if str(file_ids).isdigit() else []
        body = {
            "fileIdList": [{"FileId": fid} for fid in ids],
            "parentFileId": int(parent_id or 0),
        }
        return self.request("file/mod_pid", method="POST", json=body)

    def rename(self, file_id: Union[int, str], new_name: str) -> dict:
        """重命名文件或目录。"""
        body = {
            "driveId": 0,
            "fileId": int(file_id),
            "fileName": str(new_name),
        }
        return self.request("file/rename", method="POST", json=body)

    def trash(self, file_ids: Union[int, str, List[Union[int, str]]]) -> dict:
        """移入回收站。"""
        if isinstance(file_ids, (list, tuple, set)):
            ids = [int(fid) for fid in file_ids if str(fid).isdigit()]
        else:
            ids = [int(file_ids)] if str(file_ids).isdigit() else []
        body = {
            "driveId": 0,
            "operation": True,
            "fileTrashInfoList": [{"FileId": fid} for fid in ids],
        }
        return self.request("file/trash", method="POST", json=body)

    def list_share(
            self,
            share_key: str,
            share_pwd: str = "",
            parent_id: Union[int, str] = 0,
            page: int = 1,
            limit: int = 100,
            order_by: str = "file_name",
            order_direction: str = "asc",
    ) -> dict:
        """查询分享文件列表。"""
        params = {
            "limit": int(limit),
            "next": 0,
            "orderBy": str(order_by),
            "orderDirection": str(order_direction),
            "Page": int(page),
            "parentFileId": int(parent_id or 0),
            "ShareKey": str(share_key),
            "SharePwd": str(share_pwd or ""),
        }
        return self.request("share/get", method="GET", params=params)

    def transfer_share(
            self,
            share_key: str,
            share_pwd: str = "",
            file_list: Optional[List[Mapping[str, Any]]] = None,
            parent_id: Union[int, str] = 0,
    ) -> dict:
        """从分享转存文件到网盘。"""
        normalized = []
        for item in (file_list or []):
            normalized.append({
                "drive_id": 0,
                "file_id": item.get("file_id") or item.get("id"),
                "file_name": item.get("file_name") or item.get("name"),
                "etag": item.get("etag") or item.get("md5") or "",
                "size": int(item.get("size") or 0),
                "type": int(item.get("type") or 0),
                "parent_file_id": int(parent_id or 0),
                "s3_key_flag": item.get("s3_key_flag") or "",
            })
        body = {
            "share_key": str(share_key),
            "share_pwd": str(share_pwd or ""),
            "current_level": 1,
            "event": "transfer",
            "file_list": normalized,
        }
        return self.request("file/copy/async", method="POST", json=body)

    def add_offline(self, url: str, upload_dir: Union[int, str] = 0) -> dict:
        """添加离线下载任务。"""
        body = {
            "url": str(url).strip(),
            "upload_dir": int(upload_dir or 0),
        }
        return self.request("offline_download/upload/seed", method="POST", json=body)

    def list_offline(self, page: int = 1, page_size: int = 100, status_arr: Optional[List[int]] = None) -> dict:
        """查询离线任务列表。"""
        body = {
            "current_page": int(page),
            "page_size": int(page_size),
            "status_arr": status_arr if status_arr is not None else [0, 1, 2, 3, 4],
        }
        return self.request("offline_download/task/list", method="POST", json=body)

    def delete_offline(self, task_ids: Union[int, str, List[Union[int, str]]]) -> dict:
        """删除离线下载任务。"""
        if isinstance(task_ids, (list, tuple, set)):
            ids = [int(x) if str(x).isdigit() else str(x) for x in task_ids]
        else:
            ids = [int(task_ids) if str(task_ids).isdigit() else str(task_ids)]
        return self.request("offline_download/task/delete", method="POST", json={"task_ids": ids})

    def abort_offline(self, task_ids: Union[int, str, List[Union[int, str]]], is_abort: bool = False) -> dict:
        """重试或终止离线任务。"""
        if isinstance(task_ids, (list, tuple, set)):
            ids = [int(x) if str(x).isdigit() else str(x) for x in task_ids]
        else:
            ids = [int(task_ids) if str(task_ids).isdigit() else str(task_ids)]
        body = {
            "task_ids": ids,
            "is_abort": bool(is_abort),
            "all": False,
        }
        return self.request("offline_download/task/abort", method="POST", json=body)

    def init_upload(self, filename: str, size: int, etag: str, parent_id: Union[int, str] = 0) -> dict:
        """请求秒传与上传初始化。"""
        body = {
            "driveId": 0,
            "duplicate": 2,
            "etag": str(etag or "").lower(),
            "fileName": str(filename),
            "parentFileId": int(parent_id or 0),
            "size": int(size),
            "type": 0,
        }
        return self.request("file/upload_request", method="POST", json=body)

    def get_upload_url(self, bucket: str, key: str, upload_id: str, storage_node: str = "") -> dict:
        """获取单分片上传授权。"""
        body = {
            "bucket": bucket,
            "key": key,
            "partNumberEnd": 1,
            "partNumberStart": 1,
            "uploadId": upload_id,
            "StorageNode": storage_node,
        }
        return self.request("file/s3_upload_object/auth", method="POST", json=body)

    def get_upload_urls(
            self, bucket: str, key: str, upload_id: str, start_part: int, end_part: int, storage_node: str = ""
    ) -> dict:
        """批量获取多分片预签名上传地址。"""
        body = {
            "bucket": bucket,
            "key": key,
            "partNumberEnd": int(end_part),
            "partNumberStart": int(start_part),
            "uploadId": upload_id,
            "StorageNode": storage_node,
        }
        return self.request("file/s3_repare_upload_parts_batch", method="POST", json=body)

    def complete_upload(
            self, file_id: Union[int, str], upload_id: str, bucket: str, key: str, is_multipart: bool = False,
            storage_node: str = ""
    ) -> dict:
        """通知上传完成。"""
        body = {
            "fileId": int(file_id or 0),
            "uploadId": upload_id,
            "bucket": bucket,
            "key": key,
            "isMultipart": bool(is_multipart),
            "StorageNode": storage_node,
        }
        return self.request("file/upload_complete/v2", method="POST", json=body)


class P123ClientManager:
    """按需创建 123 原生客户端，提供统一速率限制与代理。"""

    _login_rate_limiter = DriveRateLimiter(min_interval=0.8)

    def __init__(
            self,
            token: str = "",
            timeout: int = 30,
    ):
        self.token = str(token or "").strip().removeprefix("Bearer ").strip()
        self.timeout = max(5, min(int(timeout or 30), 300))
        self._client: Optional[P123Client] = None
        self._lock = RLock()
        self.rate_limiter = DriveRateLimiter.shared(
            "p123", self.token, min_interval=0.5
        )

    def _create_client(self) -> P123Client:
        return P123Client(
            token=self.token,
            timeout=self.timeout,
        )

    def _get_client(self) -> P123Client:
        if not self.token:
            raise RuntimeError("请扫码登录 123 网盘")
        with self._lock:
            if self._client is None:
                self._client = self._create_client()
            return self._client

    def __getattr__(self, name: str):
        attr = getattr(self._get_client(), name)
        if not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            method = getattr(self._get_client(), name)

            def invoke():
                return method(*args, **kwargs)

            return self.rate_limiter.call(
                invoke,
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
            )

        return wrapped

    def create_qrcode_login(self, client_type: str = "") -> Dict[str, Any]:
        """创建扫码会话。"""
        response = self._login_rate_limiter.call(
            P123Client.login_qrcode_generate,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        check_response(response)
        data = response.get("data") or {}
        uni_id = str(data.get("uniID") or "").strip()
        login_url = str(data.get("url") or "").strip()
        if not uni_id or not login_url:
            raise RuntimeError("123网盘未返回完整的二维码登录参数")
        separator = "&" if "?" in login_url else "?"
        return {
            "uni_id": uni_id,
            "qr_url": (
                f"{login_url}{separator}env=production&uniID={uni_id}"
                "&source=123pan&type=login"
            ),
            "expires_in": 300,
            "interval": 1,
        }

    def check_qrcode_login(self, **kwargs: Any) -> Dict[str, Any]:
        """轮询扫码结果。"""
        uni_id = str(kwargs.get("uni_id") or kwargs.get("uniID") or "").strip()
        if not uni_id:
            raise ValueError("缺少 123 网盘扫码会话参数")
        response = self._login_rate_limiter.call(
            P123Client.login_qrcode_result,
            uni_id,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        if not isinstance(response, dict):
            raise RuntimeError("123网盘扫码状态响应格式无效")
        data = response.get("data") or {}
        code = safe_int(response.get("code"))
        login_status = safe_int(data.get("loginStatus"))
        if code == 200:
            token = str(data.get("token") or "").strip()
            if not token:
                raise RuntimeError("请使用 123 云盘 App 扫码登录")
            return {
                "status": "success",
                "message": "登录成功",
                "token": token,
            }
        if code != 0:
            check_response(response)
        status_map = {
            0: {"status": "waiting", "message": "等待扫码"},
            1: {"status": "scanned", "message": "已扫码，等待确认"},
            2: {"status": "cancelled", "message": "已取消登录"},
            3: {"status": "waiting", "message": "正在登录"},
            4: {"status": "expired", "message": "二维码已失效"},
        }
        return status_map.get(
            login_status,
            {"status": "waiting", "message": "等待扫码"},
        )

    def check_login(self) -> bool:
        if not self.token:
            return False
        try:
            return is_success(self.user_info())
        except Exception:
            return False

    def get_account_info(self) -> Dict[str, Any]:
        if not self.token:
            return {"connected": False, "error": "请扫码登录 123 网盘"}
        try:
            response = self.user_info()
            if not is_success(response):
                return {
                    "connected": False,
                    "error": response.get("message") or "Token 已失效",
                }
            data = response.get("data") or {}
            total = safe_int(data.get("SpacePermanent"))
            used = safe_int(data.get("SpaceUsed"))
            return {
                "connected": True,
                "user": {
                    "name": str(
                        data.get("Nickname") or data.get("UserName") or "123用户"
                    ),
                    "avatar": str(data.get("HeadImage") or data.get("Avatar") or ""),
                    "membership_supported": False,
                    "is_vip": False,
                    "is_forever_vip": False,
                    "vip_expire_date": "",
                },
                "storage": {
                    "total": format_size(total),
                    "used": format_size(used),
                    "remaining": format_size(max(0, total - used)),
                },
            }
        except Exception as error:
            return {"connected": False, "error": str(error)}

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            client.close()
