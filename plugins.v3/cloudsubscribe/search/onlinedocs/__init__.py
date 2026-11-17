"""在线文档搜索渠道。"""

from .client import OnlineDocsClient, is_online_document_url, parse_online_document
from .provider import create_onlinedocs_provider
from .service import OnlineDocumentSearchService

__all__ = [
    "OnlineDocsClient",
    "OnlineDocumentSearchService",
    "create_onlinedocs_provider",
    "is_online_document_url",
    "parse_online_document",
]
