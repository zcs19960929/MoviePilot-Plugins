"""搜索渠道动态发现、自描述规范注册与组装。"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from app.log import logger

from ..core.definitions import SearchSourceDefinition


class SearchSourceRegistry:
    """搜索渠道注册中心：负责自动发现各渠道的自描述规范与组装。"""

    _definitions: Dict[str, Type[SearchSourceDefinition]] = {}
    _ordered_definitions: Optional[tuple[Type[SearchSourceDefinition], ...]] = None
    _scanned: bool = False

    @classmethod
    def register(cls, definition_cls: Type[SearchSourceDefinition]) -> None:
        """显式注册一个搜索渠道定义。"""
        if definition_cls and getattr(definition_cls, "id", None):
            cls._definitions[definition_cls.id] = definition_cls
            cls._ordered_definitions = None

    @classmethod
    def discover(cls) -> Dict[str, Type[SearchSourceDefinition]]:
        """动态扫描 search 目录下所有子包，自动发现实现了 SearchSourceDefinition 的类。"""
        if cls._scanned:
            return cls._definitions

        package_dir = Path(__file__).resolve().parent
        for item in package_dir.iterdir():
            if item.is_dir() and not item.name.startswith(("_", ".")):
                module_name = f"{__package__}.{item.name}"
                for target_mod in (f"{module_name}.definition", module_name):
                    found = False
                    try:
                        mod = importlib.import_module(target_mod)
                        for _, attr in inspect.getmembers(mod, inspect.isclass):
                            if (
                                    issubclass(attr, SearchSourceDefinition)
                                    and attr is not SearchSourceDefinition
                                    and getattr(attr, "id", None)
                            ):
                                cls._definitions[attr.id] = attr
                                found = True
                    except Exception as error:
                        logger.warning(f"加载搜索渠道定义失败：{target_mod}：{error}")
                        continue
                    if found:
                        break

        cls._scanned = True
        cls._ordered_definitions = None
        return cls._definitions

    @classmethod
    def get_definitions(cls) -> List[Type[SearchSourceDefinition]]:
        """按配置排序返回所有已注册的搜索渠道定义。"""
        cls.discover()
        if cls._ordered_definitions is None:
            cls._ordered_definitions = tuple(sorted(
                cls._definitions.values(), key=lambda d: getattr(d, "order", 100)
            ))
        return list(cls._ordered_definitions)

    @classmethod
    def get_source_catalog(cls) -> List[Dict[str, Any]]:
        """返回搜索渠道展示目录，供前端直接渲染。"""
        return [
            {
                "key": def_cls.id,
                "name": def_cls.name,
                "icon": def_cls.icon,
                "color": def_cls.color,
            }
            for def_cls in cls.get_definitions()
        ]

    @classmethod
    def get_search_schemas(cls, context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """生成所有渠道的动态表单元数据，直接下发给前端进行自适应渲染。"""
        schemas = []
        for def_cls in cls.get_definitions():
            subtab = {
                "value": def_cls.id,
                "title": def_cls.name,
                "icon": def_cls.icon,
            }
            groups = [g.to_dict() for g in def_cls.get_config_groups(context)]
            schemas.append({
                "id": def_cls.id,
                "subtab": subtab,
                "groups": groups,
            })
        return schemas

def get_search_source_definitions() -> List[Type[SearchSourceDefinition]]:
    """获取所有已发现的搜索渠道定义列表。"""
    return SearchSourceRegistry.get_definitions()
