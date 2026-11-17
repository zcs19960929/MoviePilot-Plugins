"""插件核心基础能力。"""

from .cloud import (
    CloudDriveCapability,
    CloudDriveCapabilityError,
    CloudDrivePolicy,
    CloudFile,
    CloudDriveProvider,
    CloudDriveRegistry,
    DirectoryListing,
    DirectoryLookup,
)
from .delegation import OwnerDelegator, get_component, resolve_component
from .scraper import MediaScraper
from .search import (
    SEARCH_CIRCUIT_BREAKER,
    SearchCandidate,
    SearchCapability,
    SearchCapabilityError,
    SearchCircuitBreaker,
    SearchPolicy,
    SearchProvider,
    SearchQuery,
    SearchRegistry,
    build_resolve_kwargs,
    format_search_label,
    format_search_log_prefix,
    normalize_search_candidate,
)
from .drive_manager import CloudDriveManager
from .definitions import DriverDefinition, FieldSpec, GroupSpec, SearchSourceDefinition
from .transfer import CrossDriveTransfer, CrossTransferTaskManager, LocalRapidUploadAdapter

__all__ = [
    "DriverDefinition",
    "FieldSpec",
    "GroupSpec",
    "SearchSourceDefinition",
    "CloudDriveManager",
    "OwnerDelegator",
    "CloudDriveCapability",
    "CloudDriveCapabilityError",
    "CloudDrivePolicy",
    "CloudFile",
    "CloudDriveProvider",
    "CloudDriveRegistry",
    "DirectoryListing",
    "DirectoryLookup",
    "MediaScraper",
    "SearchCandidate",
    "SearchCapability",
    "SearchCapabilityError",
    "SearchCircuitBreaker",
    "SEARCH_CIRCUIT_BREAKER",
    "SearchPolicy",
    "SearchProvider",
    "SearchQuery",
    "SearchRegistry",
    "build_resolve_kwargs",
    "format_search_label",
    "format_search_log_prefix",
    "normalize_search_candidate",
    "get_component",
    "resolve_component",
    "CrossDriveTransfer",
    "LocalRapidUploadAdapter",
    "CrossTransferTaskManager",
]
