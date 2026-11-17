"""签到渠道注册、契约缓存与前端 schema。

各渠道通过 definition.py 自动注册签到契约（build_checkin_definition），本模块只负责扫描
收集与缓存复用：渠道签到动作由各自客户端实现（统一暴露 checkin(mode)），编排、结算与
账户快照刷新由 CheckinService 负责。
"""

import copy
from typing import Any, Dict, List, Optional

from app.log import logger

from .definitions import CheckinDefinition
from ..drive.scanner import get_driver_definitions
from ..search.scanner import get_search_source_definitions

# 渠道定义与前端表单结构只依赖类级自描述，扫描一次后长期复用。
_DEFINITION_CACHE: Optional[Dict[str, CheckinDefinition]] = None
_SCHEMA_CACHE: Optional[Dict[str, Any]] = None


def _collect_checkin_definitions() -> List[CheckinDefinition]:
    """扫描网盘驱动与搜索渠道声明的签到契约。"""
    collected: List[CheckinDefinition] = []
    sources = [
        ("网盘", get_driver_definitions()),
        ("渠道", get_search_source_definitions()),
    ]
    for label, definitions in sources:
        for definition_cls in definitions:
            try:
                definition = definition_cls.get_checkin_definition()
                if isinstance(definition, CheckinDefinition):
                    collected.append(definition)
            except Exception as error:
                logger.warning(
                    f"获取{label} {getattr(definition_cls, 'id', '')} "
                    f"签到定义失败: {error}"
                )
    collected.sort(key=lambda item: item.order)
    return collected


def get_checkin_definitions() -> Dict[str, CheckinDefinition]:
    """动态扫描并收集网盘与搜索渠道中声明的签到提供方自描述（结果缓存复用）。"""
    global _DEFINITION_CACHE
    if _DEFINITION_CACHE is None:
        _DEFINITION_CACHE = {
            item.key: item for item in _collect_checkin_definitions()
        }
    return _DEFINITION_CACHE


def get_checkin_schemas() -> Dict[str, Any]:
    """生成供前端动态渲染签到时间线与各渠道配置卡片的 schemas。"""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        definitions = list(get_checkin_definitions().values())
        _SCHEMA_CACHE = {
            "providers": [defn.to_provider_spec() for defn in definitions],
            "groups": [
                defn.group.to_dict()
                for defn in definitions
                if defn.group is not None
            ],
        }
    # 每次返回深拷贝，调用方（前端 schema 组装）可自由增删字段。
    return copy.deepcopy(_SCHEMA_CACHE)
