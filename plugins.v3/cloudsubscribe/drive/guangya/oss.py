"""光鸭上传所需的最小阿里云 OSS STS 客户端。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from typing import BinaryIO, Callable, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import requests
from requests import Response, Session

ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class OssStsCredentials:
    access_key_id: str
    access_key_secret: str
    security_token: str


class GuangyaOssClient:
    """仅实现光鸭上传使用的 OSS Object 与 Multipart 接口。"""

    _SIGNED_SUBRESOURCES = frozenset({"partNumber", "uploadId", "uploads"})
    _RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
    _MIN_PART_SIZE = 100 * 1024
    _MAX_PART_COUNT = 10_000

    def __init__(
            self,
            endpoint: str,
            bucket_name: str,
            credentials: OssStsCredentials,
            *,
            session: Optional[Session] = None,
            timeout: Tuple[float, float] = (10, 180),
            max_retries: int = 3,
    ) -> None:
        self.bucket_name = str(bucket_name or "").strip()
        self.credentials = credentials
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._base_url = self._build_base_url(endpoint, self.bucket_name)

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "GuangyaOssClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _build_base_url(endpoint: str, bucket_name: str) -> str:
        if not endpoint or not bucket_name:
            raise ValueError("OSS endpoint 或 bucket 为空")
        parsed = urlsplit(
            endpoint if "://" in endpoint else f"https://{endpoint}"
        )
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OSS endpoint 格式无效")
        host = parsed.netloc
        if host.lower() != bucket_name.lower() and not host.lower().startswith(
                f"{bucket_name.lower()}."
        ):
            host = f"{bucket_name}.{host}"
        return f"{parsed.scheme}://{host}"

    @staticmethod
    def _canonical_headers(headers: Mapping[str, str]) -> str:
        values = [
            (key.lower(), " ".join(str(value).strip().split()))
            for key, value in headers.items()
            if key.lower().startswith("x-oss-")
        ]
        values.sort(key=lambda item: item[0])
        return "".join(f"{key}:{value}\n" for key, value in values)

    @classmethod
    def _canonical_resource(
            cls, bucket_name: str, object_key: str, params: Mapping[str, str]
    ) -> str:
        resource = f"/{bucket_name}/{object_key}"
        signed_params = sorted(
            (key, str(value))
            for key, value in params.items()
            if key in cls._SIGNED_SUBRESOURCES
        )
        if not signed_params:
            return resource
        query = "&".join(
            f"{key}={value}" if value else key for key, value in signed_params
        )
        return f"{resource}?{query}"

    def _authorization(
            self,
            method: str,
            object_key: str,
            headers: Mapping[str, str],
            params: Mapping[str, str],
    ) -> str:
        date_value = headers.get("x-oss-date") or headers.get("Date", "")
        string_to_sign = "\n".join((
            method.upper(),
            headers.get("Content-MD5", ""),
            headers.get("Content-Type", ""),
            date_value,
            self._canonical_headers(headers)
            + self._canonical_resource(self.bucket_name, object_key, params),
        ))
        signature = base64.b64encode(
            hmac.new(
                self.credentials.access_key_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        return f"OSS {self.credentials.access_key_id}:{signature}"

    def _signed_headers(
            self,
            method: str,
            object_key: str,
            params: Mapping[str, str],
            headers: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, str]:
        result = dict(headers or {})
        result["Date"] = formatdate(timeval=None, localtime=False, usegmt=True)
        result["x-oss-security-token"] = self.credentials.security_token
        result["Authorization"] = self._authorization(
            method, object_key, result, params
        )
        return result

    @staticmethod
    def _object_path(object_key: str) -> str:
        if not object_key:
            raise ValueError("OSS object path 为空")
        return quote(object_key, safe="/~")

    @staticmethod
    def _error_message(response: Response) -> str:
        code = ""
        message = ""
        request_id = response.headers.get("x-oss-request-id", "")
        try:
            root = ElementTree.fromstring(response.content)
            fields = {
                node.tag.rsplit("}", 1)[-1]: str(node.text or "")
                for node in root.iter()
            }
            code = fields.get("Code", "")
            message = fields.get("Message", "")
            request_id = request_id or fields.get("RequestId", "")
        except ElementTree.ParseError:
            message = response.text[:300].strip()
        detail = ": ".join(value for value in (code, message) if value)
        request_suffix = f" (RequestId: {request_id})" if request_id else ""
        return f"OSS 请求失败 HTTP {response.status_code}{': ' if detail else ''}{detail}{request_suffix}"

    def _request(
            self,
            method: str,
            object_key: str,
            *,
            params: Optional[Mapping[str, str]] = None,
            headers: Optional[Mapping[str, str]] = None,
            data: Optional[Union[bytes, BinaryIO]] = None,
            retry: bool = True,
    ) -> Response:
        request_params = dict(params or {})
        attempts = self.max_retries + 1 if retry else 1
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            if data is not None and hasattr(data, "seek"):
                data.seek(0)
            request_headers = self._signed_headers(
                method, object_key, request_params, headers
            )
            try:
                response = self._session.request(
                    method,
                    f"{self._base_url}/{self._object_path(object_key)}",
                    params=request_params,
                    headers=request_headers,
                    data=data,
                    timeout=self.timeout,
                )
                if 200 <= response.status_code < 300:
                    return response
                error_message = self._error_message(response)
                response.close()
                if response.status_code not in self._RETRYABLE_STATUS_CODES:
                    raise RuntimeError(error_message)
                last_error = RuntimeError(error_message)
            except requests.RequestException as error:
                last_error = error
            if attempt < attempts - 1:
                time.sleep(min(0.5 * (2 ** attempt), 2.0))
        raise RuntimeError(f"OSS 请求重试后仍失败：{last_error}") from last_error

    @staticmethod
    def _xml_value(content: bytes, field_name: str) -> str:
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as error:
            raise RuntimeError("OSS 返回的 XML 格式无效") from error
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] == field_name:
                return str(node.text or "").strip()
        return ""

    @staticmethod
    def _complete_body(parts: List[Tuple[int, str]]) -> bytes:
        root = ElementTree.Element("CompleteMultipartUpload")
        for part_number, etag in parts:
            part = ElementTree.SubElement(root, "Part")
            ElementTree.SubElement(part, "PartNumber").text = str(part_number)
            ElementTree.SubElement(part, "ETag").text = etag
        return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)

    @classmethod
    def _determine_part_size(cls, file_size: int, preferred_size: int) -> int:
        part_size = max(1, int(preferred_size))
        while (
                part_size < cls._MIN_PART_SIZE
                or part_size * cls._MAX_PART_COUNT < file_size
        ):
            part_size *= 2
        return part_size

    def upload_file(
            self,
            source: Path,
            object_key: str,
            *,
            part_size: int = 5 * 1024 * 1024,
            multipart_threshold: int = 10 * 1024 * 1024,
            progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        file_size = source.stat().st_size
        if file_size < multipart_threshold:
            with source.open("rb") as source_file:
                self._request(
                    "PUT",
                    object_key,
                    headers={"Content-Type": "application/octet-stream"},
                    data=source_file,
                )
            if progress_callback:
                progress_callback(file_size, file_size)
            return

        init_response = self._request(
            "POST", object_key, params={"uploads": ""}
        )
        upload_id = self._xml_value(init_response.content, "UploadId")
        if not upload_id:
            raise RuntimeError("OSS 初始化分片上传未返回 UploadId")

        part_size = self._determine_part_size(file_size, part_size)
        parts: List[Tuple[int, str]] = []
        transferred = 0
        try:
            with source.open("rb") as source_file:
                part_number = 1
                while chunk := source_file.read(part_size):
                    response = self._request(
                        "PUT",
                        object_key,
                        params={
                            "partNumber": str(part_number),
                            "uploadId": upload_id,
                        },
                        data=chunk,
                    )
                    etag = response.headers.get("ETag") or response.headers.get("Etag")
                    if not etag:
                        raise RuntimeError(f"OSS 第 {part_number} 个分片未返回 ETag")
                    parts.append((part_number, etag))
                    transferred += len(chunk)
                    if progress_callback:
                        progress_callback(transferred, file_size)
                    part_number += 1

            body = self._complete_body(parts)
            content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
            self._request(
                "POST",
                object_key,
                params={"uploadId": upload_id},
                headers={
                    "Content-MD5": content_md5,
                    "Content-Type": "application/xml",
                },
                data=body,
            )
        except Exception:
            try:
                self._request(
                    "DELETE",
                    object_key,
                    params={"uploadId": upload_id},
                    retry=False,
                )
            except Exception:
                pass
            raise
