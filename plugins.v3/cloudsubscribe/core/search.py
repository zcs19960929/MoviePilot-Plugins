"""搜索渠道能力规范与运行时注册表。"""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    TypedDict,
    Union,
    runtime_checkable,
)

from app.log import logger


class SearchCapability(str, Enum):
    """搜索渠道可独立提供的能力。"""

    RESOURCE_SEARCH = "resource_search"
    RESOURCE_RESOLVE = "resource_resolve"
    RESOURCE_PREVIEW = "resource_preview"
    RESOURCE_FILE_PREVIEW = "resource_file_preview"
    RESOURCE_UNLOCK = "resource_unlock"
    ACCOUNT = "account"
    CHECKIN = "checkin"
    POINT_BUDGET = "point_budget"
    CACHE_MAINTENANCE = "cache_maintenance"
    LIFECYCLE = "lifecycle"


class SearchCapabilityError(RuntimeError):
    """当前搜索渠道不支持调用方要求的能力。"""


def format_search_label(
        mediainfo: Any, media_type: Any, season: Optional[int] = None
) -> str:
    """统一生成搜索日志中的媒体标识。"""
    title = str(getattr(mediainfo, "title", "") or "未知标题")
    year = getattr(mediainfo, "year", None)
    label = f"{title} ({year})" if year else title
    type_value = str(getattr(media_type, "value", media_type) or "").lower()
    if type_value in {"tv", "电视剧"} and season is not None:
        label += f" S{int(season):02d}"
    return label


def format_search_log_prefix(query: "SearchQuery", source: str) -> str:
    """统一生成 Provider 搜索日志前缀。"""
    label = format_search_label(query.mediainfo, query.media_type, query.season)
    return f"[{label}][{str(source or 'unknown').strip().upper()}]"


class SearchCandidate(TypedDict, total=False):
    """搜索层向订阅流程输出的统一候选字段。"""

    url: Union[str, List[str]]
    title: str
    description: str
    size: int
    update_time: str
    resource_type: str
    source: str
    source_url: str
    media_page_url: str
    resource_ref: str
    need_unlock: bool
    need_access: bool
    unlock_points: int
    is_free: bool
    is_unlocked: bool
    preview_episodes: Dict[str, List[int]]
    preview_episodes_authoritative: bool
    identity_verified: bool
    target_season: Optional[int]
    target_episodes: List[int]
    supports_file_preview: bool
    provider_data: Dict[str, Any]


def normalize_search_candidate(
        candidate: Mapping[str, Any], source: str
) -> SearchCandidate:
    """在 Provider 边界统一候选公共字段，不泄露渠道命名。"""
    result = dict(candidate or {})
    normalized_source = str(source or result.get("source") or "").strip().lower()
    resource_type = str(result.get("resource_type") or "").strip().lower()
    result["source"] = normalized_source
    result["resource_type"] = resource_type
    raw_url = result.get("url") or ""
    if isinstance(raw_url, (list, tuple)):
        result["url"] = [
            str(value).strip() for value in raw_url
            if str(value or "").strip()
        ]
    else:
        result["url"] = str(raw_url).strip()
    result["resource_ref"] = str(result.get("resource_ref") or "").strip()
    result.pop("pan_type", None)
    result.pop("share_url", None)
    result.pop("slug", None)
    for key in (
            "title", "description", "update_time", "source_url",
            "media_page_url", "resource_ref",
    ):
        result[key] = str(result.get(key) or "").strip()
    try:
        result["unlock_points"] = max(0, int(result.get("unlock_points") or 0))
    except (TypeError, ValueError):
        result["unlock_points"] = 0
    provider_data = result.get("provider_data")
    result["provider_data"] = (
        dict(provider_data) if isinstance(provider_data, Mapping) else {}
    )
    result["identity_verified"] = bool(result.get("identity_verified"))
    try:
        target_season = int(result.get("target_season") or 0)
    except (TypeError, ValueError):
        target_season = 0
    result["target_season"] = target_season if target_season > 0 else None
    return result


def build_resolve_kwargs(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    """候选字典 → RESOURCE_RESOLVE 入参（预览与转存共用）。"""
    row = dict(candidate or {})
    provider_data = row.get("provider_data")
    provider_data = dict(provider_data) if isinstance(provider_data, Mapping) else {}
    reference = str(row.get("resource_ref") or "").strip()
    return {
        "resource_id": str(
            provider_data.get("resource_id") or row.get("resource_id") or reference
        ).strip(),
        "token": str(provider_data.get("token") or reference).strip(),
        "password": str(
            provider_data.get("password") or row.get("password") or ""
        ).strip(),
        "resource_type": str(row.get("resource_type") or "").strip(),
        "kind": str(provider_data.get("kind") or "").strip(),
        "seed_id": str(provider_data.get("seed_id") or "").strip(),
        "path": str(provider_data.get("path") or "").strip(),
        "host": str(provider_data.get("host") or "").strip(),
        "title": str(row.get("title") or "").strip(),
        "provider_data": provider_data,
    }


@dataclass(frozen=True)
class SearchQuery:
    """渠道搜索统一输入；来源私有参数由适配器从上下文读取。"""

    mediainfo: Any
    media_type: Any
    season: Optional[int] = None
    target_episodes: tuple[int, ...] = ()
    target_episode_air_dates: Mapping[int, str] = field(default_factory=dict)
    subscribe: Any = field(default=None, repr=False, compare=False)
    resource_list_mode: bool = False
    result_limit: Optional[int] = None
    keyword: Optional[str] = None


@dataclass(frozen=True)
class SearchPolicy:
    """渠道级缓存、测试和并发边界。"""

    cacheable: bool = True
    cache_empty_results: bool = True
    cache_context: Mapping[str, Any] = field(default_factory=dict)
    supports_test: bool = True
    max_results: int = 0
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_results", max(0, int(self.max_results or 0)))
        object.__setattr__(
            self, "max_concurrency", max(1, int(self.max_concurrency or 1))
        )
        object.__setattr__(self, "cache_context", dict(self.cache_context or {}))


@runtime_checkable
class ResourceSearchOperations(Protocol):
    def search(self, query: SearchQuery) -> List[SearchCandidate]: ...


@runtime_checkable
class ResourceResolveOperations(Protocol):
    def resolve(self, **kwargs: Any) -> SearchCandidate: ...


@runtime_checkable
class ResourcePreviewOperations(Protocol):
    def preview(self, candidate: Mapping[str, Any]) -> SearchCandidate: ...


@runtime_checkable
class ResourceFilePreviewOperations(Protocol):
    """渠道级文件预览契约：按渠道自身数据形态返回可展示的文件列表。"""

    def preview_files(self, payload: Mapping[str, Any]) -> Dict[str, Any]: ...


@runtime_checkable
class ResourceUnlockOperations(Protocol):
    def unlock(
            self, candidate: Mapping[str, Any], search_label: str = ""
    ) -> Any: ...


@runtime_checkable
class SearchAccountOperations(Protocol):
    def get_account_info(self) -> Dict[str, Any]: ...


@runtime_checkable
class SearchCheckinOperations(Protocol):
    """签到能力的统一契约：渠道只按模式入参，不暴露渠道私有参数。"""

    def checkin(self, mode: str = "normal") -> Dict[str, Any]: ...


@runtime_checkable
class SearchPointBudgetOperations(Protocol):
    def configure_storage(self, get_data: Any, save_data: Any) -> None: ...

    def reset_task(self) -> None: ...

    def reset_subscription(self, key: str = "") -> int: ...

    def clear_subscription(self, key: str) -> bool: ...

    def has_budget(self, points: Any) -> bool: ...

    def clear_history(self) -> int: ...

    def clear_cached_urls(self) -> int: ...


@runtime_checkable
class SearchCacheOperations(Protocol):
    def clear_cache(self) -> Any: ...


@runtime_checkable
class SearchLifecycleOperations(Protocol):
    def close(self) -> None: ...


CAPABILITY_CONTRACTS = {
    SearchCapability.RESOURCE_SEARCH: ResourceSearchOperations,
    SearchCapability.RESOURCE_RESOLVE: ResourceResolveOperations,
    SearchCapability.RESOURCE_PREVIEW: ResourcePreviewOperations,
    SearchCapability.RESOURCE_FILE_PREVIEW: ResourceFilePreviewOperations,
    SearchCapability.RESOURCE_UNLOCK: ResourceUnlockOperations,
    SearchCapability.ACCOUNT: SearchAccountOperations,
    SearchCapability.CHECKIN: SearchCheckinOperations,
    SearchCapability.POINT_BUDGET: SearchPointBudgetOperations,
    SearchCapability.CACHE_MAINTENANCE: SearchCacheOperations,
    SearchCapability.LIFECYCLE: SearchLifecycleOperations,
}


def _implements_contract(service: Any, contract: type) -> bool:
    for name, declaration in contract.__dict__.items():
        if name.startswith("_"):
            continue
        value = getattr(service, name, None)
        if callable(declaration) and not callable(value):
            return False
    return True


@dataclass(frozen=True)
class SearchProvider:
    """一个搜索渠道及其实际具备的能力集合。"""

    key: str
    name: str
    services: Mapping[SearchCapability, Any]
    resource_types: FrozenSet[str] = frozenset()
    policy: SearchPolicy = field(default_factory=SearchPolicy)

    def __post_init__(self) -> None:
        key = str(self.key or "").strip().lower()
        if not key:
            raise ValueError("搜索渠道 key 不能为空")
        object.__setattr__(self, "key", key)
        object.__setattr__(
            self,
            "resource_types",
            frozenset(
                str(value or "").strip().lower()
                for value in self.resource_types
                if str(value or "").strip()
            ),
        )
        services = dict(self.services)
        object.__setattr__(self, "services", services)
        for capability, service in services.items():
            contract = CAPABILITY_CONTRACTS.get(capability)
            if contract is None:
                raise ValueError(f"未定义的搜索能力：{capability}")
            if not _implements_contract(service, contract):
                raise TypeError(
                    f"{self.name}的 {capability.value} 服务不符合接口 "
                    f"{contract.__name__}"
                )

    @property
    def capabilities(self) -> FrozenSet[SearchCapability]:
        return frozenset(self.services)

    def supports(self, capability: SearchCapability) -> bool:
        return capability in self.services

    def supports_resource_type(self, resource_type: str) -> bool:
        return str(resource_type or "").strip().lower() in self.resource_types

    def require(self, capability: SearchCapability) -> Any:
        service = self.services.get(capability)
        if service is None:
            raise SearchCapabilityError(
                f"{self.name}不支持能力：{capability.value}"
            )
        return service

    def search(self, query: SearchQuery) -> List[SearchCandidate]:
        rows = self.require(SearchCapability.RESOURCE_SEARCH).search(query)
        return [
            normalize_search_candidate(row, self.key)
            for row in (rows or [])
            if isinstance(row, Mapping)
        ]

    def resolve(self, **kwargs: Any) -> SearchCandidate:
        row = self.require(SearchCapability.RESOURCE_RESOLVE).resolve(**kwargs)
        return normalize_search_candidate(row or {}, self.key)

    def preview(self, candidate: Mapping[str, Any]) -> SearchCandidate:
        row = self.require(SearchCapability.RESOURCE_PREVIEW).preview(candidate)
        return normalize_search_candidate(row or {}, self.key)

    def preview_files(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        row = self.require(SearchCapability.RESOURCE_FILE_PREVIEW).preview_files(
            payload
        )
        return dict(row or {})

    def unlock(
            self, candidate: Mapping[str, Any], search_label: str = ""
    ) -> Any:
        if self.supports(SearchCapability.RESOURCE_UNLOCK):
            return self.require(SearchCapability.RESOURCE_UNLOCK).unlock(
                candidate, search_label=search_label
            )
        # 延迟解析渠道：失败按"无链接"跳过，不中断订阅。
        try:
            row = self.resolve(**build_resolve_kwargs(candidate))
        except Exception as error:
            logger.warning(f"{self.name}资源解析失败，跳过该候选：{error}")
            return ""
        return row.get("url") or ""

    def clear_cache(self) -> Any:
        return self.require(SearchCapability.CACHE_MAINTENANCE).clear_cache()

    def close(self) -> None:
        self.require(SearchCapability.LIFECYCLE).close()


class SearchRegistry:
    """按稳定 key 管理搜索渠道，不在调用端维护实现类映射。"""

    def __init__(self, providers: Iterable[SearchProvider] = ()):
        self._providers: Dict[str, SearchProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: SearchProvider, replace: bool = False) -> None:
        key = str(provider.key or "").strip().lower()
        if key in self._providers and not replace:
            raise ValueError(f"搜索渠道重复注册：{key}")
        self._providers[key] = provider

    def __contains__(self, key: str) -> bool:
        return str(key or "").strip().lower() in self._providers

    def has(self, key: str) -> bool:
        return str(key or "").strip().lower() in self._providers

    def get(self, key: str, default: Optional[SearchProvider] = None) -> Optional[SearchProvider]:
        return self._providers.get(str(key or "").strip().lower(), default)

    def require(self, key: str) -> SearchProvider:
        normalized = str(key or "").strip().lower()
        provider = self._providers.get(normalized)
        if provider is None:
            raise KeyError(f"搜索渠道未注册：{normalized or '<empty>'}")
        return provider

    def available(self) -> List[SearchProvider]:
        return list(self._providers.values())



class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class SearchCircuitBreaker:
    """搜索渠道熔断器，防止故障、超时或风控渠道拖垮全局搜索调度。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._states: Dict[str, CircuitState] = {}
        self._failure_counts: Dict[str, int] = {}
        self._open_untils: Dict[str, float] = {}
        self._reasons: Dict[str, str] = {}
        self._enabled: bool = True
        self._failure_threshold: int = 3
        self._cooldown_seconds: float = 60.0

    def configure(
            self,
            enabled: bool = True,
            failure_threshold: int = 3,
            cooldown_seconds: float = 60.0,
    ) -> None:
        """配置熔断器全局策略。"""
        with self._lock:
            self._enabled = bool(enabled)
            self._failure_threshold = max(1, int(failure_threshold or 3))
            self._cooldown_seconds = max(5.0, float(cooldown_seconds or 60.0))

    def get_state(self, key: str) -> CircuitState:
        """获取渠道当前熔断状态。"""
        normalized = str(key or "").strip().lower()
        with self._lock:
            state = self._states.get(normalized, CircuitState.CLOSED)
            if state == CircuitState.OPEN and time.monotonic() >= self._open_untils.get(normalized, 0.0):
                return CircuitState.HALF_OPEN
            return state

    def can_execute(self, key: str) -> bool:
        if not self._enabled:
            return True
        normalized = str(key or "").strip().lower()
        with self._lock:
            state = self._states.get(normalized, CircuitState.CLOSED)
            if state == CircuitState.CLOSED:
                return True
            now = time.monotonic()
            open_until = self._open_untils.get(normalized, 0.0)
            if now >= open_until:
                self._states[normalized] = CircuitState.HALF_OPEN
                return True
            return False

    def cooldown_remaining(self, key: str) -> float:
        normalized = str(key or "").strip().lower()
        with self._lock:
            state = self._states.get(normalized, CircuitState.CLOSED)
            if state == CircuitState.CLOSED:
                return 0.0
            return max(0.0, self._open_untils.get(normalized, 0.0) - time.monotonic())

    def get_cooldown_remaining(self, key: str) -> float:
        """别名方法：获取剩余冷却秒数。"""
        return self.cooldown_remaining(key)

    def get_reason(self, key: str) -> str:
        normalized = str(key or "").strip().lower()
        with self._lock:
            return self._reasons.get(normalized, "")

    def record_success(self, key: str) -> None:
        normalized = str(key or "").strip().lower()
        with self._lock:
            self._failure_counts[normalized] = 0
            self._states[normalized] = CircuitState.CLOSED
            self._open_untils.pop(normalized, None)
            self._reasons.pop(normalized, None)

    def record_failure(
            self,
            key: str,
            reason: str = "",
            threshold: Optional[int] = None,
            cooldown_seconds: Optional[float] = None,
    ) -> None:
        if not self._enabled:
            return
        normalized = str(key or "").strip().lower()
        max_threshold = max(1, int(threshold or self._failure_threshold))
        cooldown = max(5.0, float(cooldown_seconds or self._cooldown_seconds))
        with self._lock:
            count = self._failure_counts.get(normalized, 0) + 1
            self._failure_counts[normalized] = count
            if count >= max_threshold:
                self.trip(normalized, cooldown, reason=reason or f"连续失败 {count} 次")

    def trip(self, key: str, cooldown_seconds: float, reason: str = "风控保护") -> None:
        normalized = str(key or "").strip().lower()
        cooldown = max(1.0, float(cooldown_seconds or self._cooldown_seconds))
        with self._lock:
            self._states[normalized] = CircuitState.OPEN
            self._open_untils[normalized] = time.monotonic() + cooldown
            self._reasons[normalized] = str(reason or "风控保护")
        logger.warning(
            f"[{normalized.upper()}] 触发搜索熔断（原因：{reason}），"
            f"将在接下来的 {int(cooldown + 0.999)} 秒内短路跳过该渠道查询"
        )

    def reset(self, key: str = "") -> None:
        with self._lock:
            if key:
                normalized = str(key).strip().lower()
                self._states.pop(normalized, None)
                self._failure_counts.pop(normalized, None)
                self._open_untils.pop(normalized, None)
                self._reasons.pop(normalized, None)
            else:
                self._states.clear()
                self._failure_counts.clear()
                self._open_untils.clear()
                self._reasons.clear()


SEARCH_CIRCUIT_BREAKER = SearchCircuitBreaker()
