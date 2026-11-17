"""小雅免 Docker 离线索引与网盘分享搜索渠道包。"""

from .client import XiaoyaClient, XiaoyaError
from .definition import XiaoyaSourceDefinition
from .provider import create_xiaoya_provider
from .resource import XiaoyaResourceService
from .service import XiaoyaSearchService

__all__ = [
    "XiaoyaClient",
    "XiaoyaError",
    "XiaoyaResourceService",
    "XiaoyaSearchService",
    "XiaoyaSourceDefinition",
    "create_xiaoya_provider",
]
