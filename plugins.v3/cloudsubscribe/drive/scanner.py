"""网盘驱动动态发现、自描述规范注册与组装。"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from app.log import logger

from ..core.definitions import DriverDefinition, FieldSpec, GroupSpec


class DriverRegistry:
    """网盘驱动注册中心：负责自动发现各网盘驱动的自描述规范与元数据。"""

    _definitions: Dict[str, Type[DriverDefinition]] = {}
    _scanned: bool = False

    @classmethod
    def register(cls, definition_cls: Type[DriverDefinition]) -> None:
        """显式注册一个网盘驱动规范。"""
        if definition_cls and getattr(definition_cls, "id", None):
            cls._definitions[definition_cls.id] = definition_cls

    @classmethod
    def discover(cls) -> Dict[str, Type[DriverDefinition]]:
        """动态扫描 drive 目录下所有子包，自动发现实现了 DriverDefinition 的类。"""
        if cls._scanned:
            return cls._definitions

        package_dir = Path(__file__).resolve().parent
        for item in package_dir.iterdir():
            if item.is_dir() and not item.name.startswith(("_", ".")):
                module_name = f"{__package__}.{item.name}"
                for target_mod in (f"{module_name}.definition", module_name):
                    try:
                        mod = importlib.import_module(target_mod)
                        for _, attr in inspect.getmembers(mod, inspect.isclass):
                            if (
                                    issubclass(attr, DriverDefinition)
                                    and attr is not DriverDefinition
                                    and getattr(attr, "id", None)
                            ):
                                cls._definitions[attr.id] = attr
                    except (ImportError, AttributeError) as error:
                        logger.warning(f"加载网盘驱动定义失败：{target_mod}：{error}")
                        continue

        cls._scanned = True
        return cls._definitions

    @classmethod
    def get_definitions(cls) -> List[Type[DriverDefinition]]:
        """按配置排序返回所有已注册的网盘驱动定义。"""
        cls.discover()
        return sorted(cls._definitions.values(), key=lambda d: getattr(d, "order", 100))

    @classmethod
    def get_driver_schemas(cls, context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """生成所有网盘驱动的动态表单元数据，直接下发给前端进行自适应渲染。"""
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


def get_driver_definitions() -> List[Type[DriverDefinition]]:
    """获取所有已发现的网盘驱动定义列表。"""
    return DriverRegistry.get_definitions()
