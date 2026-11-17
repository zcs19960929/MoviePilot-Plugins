"""STRM 内容与本地目标路径生成。"""
import re
from pathlib import Path, PurePosixPath
from string import Formatter
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import quote, urlparse


class StrmTemplateError(ValueError):
    """STRM 模板配置错误。"""


class StrmGenerator:
    """按固定变量渲染 STRM URL，并写入本地挂载目录。"""

    CORE_VARIABLES = {"base_url", "file_name", "file_path"}
    DEFAULT_BASE_URL = "http://172.17.0.1:9527"
    DEFAULT_TEMPLATE = "{base_url}{file_path}"
    # 播放代理路由（插件 API 全路径）与其 STRM 模板
    PROXY_DEFAULT_ROUTE = "d"
    # 可按网盘标识独立解析直链的变量（按此优先级生成短链）
    PROXY_IDENTIFIER_VARIABLES = ("pickcode", "file_id")

    @classmethod
    def proxy_identifier_variable(
            cls, reference: Any = None, provider_variables: Any = ()
    ) -> str:
        """可用于代理短链定位的标识变量（以网盘声明的 proxy_identifier 为准）。"""
        declared = str(getattr(reference, "proxy_identifier", "") or "").strip()
        if declared:
            return declared
        variables = provider_variables or getattr(reference, "template_variables", ())
        available = {str(value or "").strip() for value in (variables or ())}
        for name in cls.PROXY_IDENTIFIER_VARIABLES:
            if name in available:
                return name
        return ""

    @classmethod
    def with_port(cls, base_url: Any, port: Any = 0) -> str:
        """把监听端口并入基础地址；基础地址已显式带端口时保持不变。"""
        base = str(base_url or "").strip().rstrip("/") or cls.DEFAULT_BASE_URL
        try:
            port_number = int(port or 0)
        except (TypeError, ValueError):
            port_number = 0
        if port_number <= 0 or urlparse(base).port:
            return base
        return f"{base}:{port_number}"

    @classmethod
    def resolve_base_url(cls, base_url: Any, template: Any, port: Any = 0) -> str:
        """按模板决定基址端口：代理短链模板必须带上监听端口，直链模板保持原样。"""
        if any(
                f"{{{name}}}" in str(template or "")
                for name in cls.PROXY_IDENTIFIER_VARIABLES
        ):
            return cls.with_port(base_url, port)
        return (
                str(base_url or "").strip().rstrip("/") or cls.DEFAULT_BASE_URL
        )

    @classmethod
    def proxy_route_segment(cls, value: Any = "") -> str:
        """规范化短链路由段：仅允许单段安全字符，默认 ``d``。"""
        segment = str(value or "").strip().strip("/").split("/")[-1]
        if not segment or not re.fullmatch(r"[A-Za-z0-9._-]+", segment):
            return cls.PROXY_DEFAULT_ROUTE
        return segment

    @classmethod
    def proxy_template(
            cls, identifier_variable: str = "", route: Any = ""
    ) -> str:
        """代理短链模板；无可用标识变量时返回空串（该网盘不支持短链代理）。"""
        if not identifier_variable:
            return ""
        segment = cls.proxy_route_segment(route)
        return (
            f"{{base_url}}/{segment}/{{{identifier_variable}}}"
            "?/{file_name}"
        )

    @classmethod
    def build_proxy_template(
            cls, base_url: str, identifier_variable: str = "", route: Any = ""
    ) -> Tuple[str, str]:
        """播放代理的 STRM 基址与短链模板；基址复用 STRM 基础地址。"""
        normalized = str(base_url or "").strip().rstrip("/") or cls.DEFAULT_BASE_URL
        return normalized, cls.proxy_template(identifier_variable, route)

    def __init__(
            self,
            base_url: str,
            template: str,
            provider_variables: Iterable[str] = (),
    ):
        self.base_url = str(
            self.DEFAULT_BASE_URL if base_url is None else base_url
        ).strip().rstrip("/")
        self.template = str(
            self.DEFAULT_TEMPLATE if template is None else template
        ).strip()
        self.allowed_variables = self.CORE_VARIABLES | {
            str(value or "").strip()
            for value in provider_variables
            if str(value or "").strip()
        }
        self._template_variables = set()
        self._validate_template()

    def _validate_template(self) -> None:
        if not self.base_url:
            raise StrmTemplateError("STRM base_url 不能为空")
        if not self.template:
            raise StrmTemplateError("STRM URL 模板不能为空")

        variables = set()
        try:
            parsed = Formatter().parse(self.template)
            for _, field_name, format_spec, conversion in parsed:
                if field_name is None:
                    continue
                if field_name not in self.allowed_variables:
                    raise StrmTemplateError(f"STRM URL 模板包含不支持的变量：{field_name}")
                if format_spec or conversion:
                    raise StrmTemplateError(f"STRM URL 模板变量不支持格式转换：{field_name}")
                variables.add(field_name)
        except ValueError as error:
            raise StrmTemplateError(f"STRM URL 模板格式错误：{error}") from error

        if not variables:
            raise StrmTemplateError("STRM URL 模板至少需要一个变量")
        self._template_variables = variables

    @staticmethod
    def _cloud_file_path(cloud_dir: str, file_name: str) -> str:
        """网盘相对路径转 URL 路径：保留 ``/`` 分隔，其余按 URL 规则编码。"""
        normalized_dir = "/" + str(cloud_dir or "/").replace("\\", "/").strip("/")
        if normalized_dir != "/":
            normalized_dir = normalized_dir.rstrip("/")
        path = (
            f"{normalized_dir.rstrip('/')}/{file_name}"
            if normalized_dir != "/" else f"/{file_name}"
        )
        return quote(path, safe="/")

    @staticmethod
    def _relative_cloud_dir(cloud_dir: str, cloud_root: str) -> PurePosixPath:
        normalized_dir = "/" + str(cloud_dir or "/").replace("\\", "/").strip("/")
        normalized_root = "/" + str(cloud_root or "/").replace("\\", "/").strip("/")
        normalized_dir = normalized_dir.rstrip("/") or "/"
        normalized_root = normalized_root.rstrip("/") or "/"

        if normalized_root == "/":
            relative = normalized_dir.lstrip("/")
        elif normalized_dir == normalized_root:
            relative = ""
        elif normalized_dir.startswith(f"{normalized_root}/"):
            relative = normalized_dir[len(normalized_root):].lstrip("/")
        else:
            raise StrmTemplateError(
                f"目标目录不在网盘根路径下：{normalized_dir}（根路径：{normalized_root}）"
            )
        return PurePosixPath(relative)

    def render(
            self,
            file_name: str,
            file_path: str,
            template_values: Optional[Mapping[str, Any]] = None,
    ) -> str:
        values: Dict[str, str] = {
            "base_url": self.base_url,
            "file_name": str(file_name or "").strip(),
            "file_path": str(file_path or "").strip(),
        }
        values.update({
            str(key): str(value or "").strip()
            for key, value in (template_values or {}).items()
            if str(key) in self.allowed_variables
        })
        if not values["file_name"]:
            raise StrmTemplateError("生成 STRM 时缺少 file_name")
        missing = sorted(
            name for name in self._template_variables if not values.get(name)
        )
        if missing:
            raise StrmTemplateError(
                f"生成 STRM 时缺少模板变量：{', '.join(missing)}"
            )
        return self.template.format_map(values)

    def local_path(
            self,
            local_root: str,
            cloud_root: str,
            cloud_dir: str,
            file_name: str,
    ) -> Path:
        """按网盘目录映射计算 STRM 本地路径，不创建目录或文件。"""
        local_root_text = str(local_root or "").strip()
        if not local_root_text:
            raise StrmTemplateError("本地挂载媒体根路径不能为空")
        local_root_path = Path(local_root_text).expanduser()
        relative_dir = self._relative_cloud_dir(cloud_dir, cloud_root)
        local_dir = local_root_path.joinpath(*relative_dir.parts)
        root_resolved = local_root_path.resolve(strict=False)
        target_resolved = local_dir.resolve(strict=False)
        if target_resolved != root_resolved and root_resolved not in target_resolved.parents:
            raise StrmTemplateError(f"STRM 目标目录越出本地挂载根路径：{target_resolved}")
        return local_dir / f"{Path(file_name).stem}.strm"

    def write(
            self,
            local_root: str,
            cloud_root: str,
            cloud_dir: str,
            file_name: str,
            template_values: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Path, str]:
        cloud_file_path = self._cloud_file_path(cloud_dir, file_name)
        content = self.render(
            file_name=file_name,
            file_path=cloud_file_path,
            template_values=template_values,
        )
        strm_path = self.local_path(
            local_root=local_root,
            cloud_root=cloud_root,
            cloud_dir=cloud_dir,
            file_name=file_name,
        )
        strm_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = strm_path.with_suffix(f"{strm_path.suffix}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(strm_path)
        return strm_path, content
