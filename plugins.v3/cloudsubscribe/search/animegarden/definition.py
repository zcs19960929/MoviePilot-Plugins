"""AnimeGarden 搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from .client import AnimeGardenClient
from .provider import create_animegarden_provider
from .service import AnimeGardenSearchService
from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition


class AnimeGardenSourceDefinition(SearchSourceDefinition):
    """AnimeGarden 搜索渠道规范。"""

    id = "animegarden"
    name = "AnimeGarden"
    icon = "mdi-flower-tulip-outline"
    color = "deep-orange-darken-1"
    order = 70

    @classmethod
    def configure_owner(cls, owner: Any, config: Mapping[str, Any]) -> None:
        value = lambda key, default=None: cls.config_value(owner.__dict__, key, default)
        owner._animegarden_base_url = str(
            value("animegarden_base_url", "https://animes.garden/")
            or "https://animes.garden/"
        ).strip()
        owner._animegarden_result_limit = max(
            1, min(int(value("animegarden_result_limit", 10) or 10), 80)
        )
        owner._animegarden_request_interval = max(
            0.2,
            min(float(value("animegarden_request_interval", 1.0) or 1.0), 10.0),
        )
        owner._animegarden_timeout = max(
            5, min(int(value("animegarden_timeout", 60) or 60), 120)
        )
        owner._animegarden_fansub_order = list(
            value("animegarden_fansub_order", []) or []
        )
        owner._animegarden_exclude_re = str(
            value("animegarden_exclude_re", "") or ""
        ).strip()
        owner._animegarden_no_subs_re = str(
            value("animegarden_no_subs_re", "") or ""
        ).strip()
        owner._animegarden_chinese_re = str(
            value("animegarden_chinese_re", "") or ""
        ).strip()

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        fansub_presets = [
            {"title": "LoliHouse", "value": "LoliHouse"},
            {"title": "VCB-Studio", "value": "VCB-Studio"},
            {"title": "喵萌奶茶屋", "value": "喵萌奶茶|Nekomoe"},
            {"title": "Nix-Raws", "value": "Nix-Raws"},
            {"title": "ANI", "value": r"\bANI\b|ANi"},
            {"title": "SweetSub", "value": "SweetSub"},
            {"title": "千夏字幕组", "value": "千夏"},
            {"title": "动漫国字幕组", "value": "动漫国|動漫國|DMG"},
            {"title": "极影字幕社", "value": "极影|極影|KTXP"},
            {"title": "樱都字幕组", "value": "桜都|樱都|櫻都|Sakurato"},
            {"title": "诸神字幕组", "value": "诸神|諸神|Kamigami"},
            {"title": "北宇治字幕组", "value": "北宇治|Kitauji"},
            {"title": "悠哈璃羽字幕社", "value": "悠哈璃羽|UHA-WINGS"},
            {"title": "爱恋字幕社", "value": "爱恋字幕|愛戀字幕|KissSub"},
            {"title": "拨雪寻春", "value": "拨雪寻春|撥雪尋春"},
            {"title": "HaruHana", "value": r"Haru[ &]+Hana"},
            {"title": "澄空学园", "value": "澄空|Sumisora"},
            {"title": "华盟字幕社", "value": "华盟|華盟|CASO"},
            {"title": "霜庭云花", "value": "霜庭云花|霜庭雲花|STYH"},
            {"title": "豌豆字幕组", "value": "豌豆|Dymy"},
            {"title": "Airota", "value": "Airota"},
            {"title": "Lilith-Raws", "value": "Lilith-Raws"},
            {"title": "DBD制作组", "value": "DBD制作组|DBD-Raws"},
            {"title": "NC-Raws", "value": r"\bNC-Raws\b"},
            {"title": "雪飘工作室", "value": "雪飘|FLsnow"},
            {"title": "幻樱字幕组", "value": "幻樱|HYSub"},
        ]

        return [
            GroupSpec(
                tab="animegarden",
                title="AnimeGarden",
                icon="mdi-flower-tulip-outline",
                fields=[
                    FieldSpec(
                        key="animegarden_base_url",
                        label="服务地址",
                        placeholder="https://animes.garden/",
                        default="https://animes.garden/",
                        cols=12,
                    ),
                    FieldSpec(
                        key="animegarden_fansub_order",
                        label="字幕组优先级偏好",
                        type="priority-order",
                        default=[
                            "LoliHouse",
                            "VCB-Studio",
                            "喵萌奶茶|Nekomoe",
                            "Nix-Raws",
                            r"\bANI\b|ANi",
                        ],
                        cols=12,
                        options=fansub_presets,
                        extra={"allowCustom": True},
                    ),
                    FieldSpec(
                        key="animegarden_exclude_re",
                        label="动漫排除正则",
                        type="text",
                        default=r"720[pP]|480[pP]|特别篇|特別篇|\b(?:SP|OVA|OAD)\d*|\b\d+\s*-\s*\d+\b",
                        cols=12,
                    ),
                    FieldSpec(
                        key="animegarden_no_subs_re",
                        label="生肉/无字幕排除正则",
                        type="text",
                        default=r"无字幕|無字幕|无字版|無字版|生肉|\b(?:unsubbed|no[ ._-]*subs?|subtitle[ ._-]*free)\b",
                        cols=12,
                    ),
                    FieldSpec(
                        key="animegarden_chinese_re",
                        label="中文字幕匹配正则",
                        type="text",
                        default=r"简[体體繁中]|簡[体體繁中]|繁[体體简簡中]|中[日英双雙文]|[简簡繁]日|\b(?:CHS|CHT|BIG5|GB|SC|TC|ZH|CHI|ZHO)(?:\b|_)",
                        cols=12,
                    ),
                    FieldSpec(
                        key="test_animegarden",
                        label="测试搜索",
                        type="test-source",
                        source="animegarden",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="animegarden",
                title="搜索与限速",
                icon="mdi-shield-search",
                fields=[
                    FieldSpec(
                        key="animegarden_result_limit",
                        label="候选上限",
                        type="number",
                        default=10,
                        min=1,
                        max=200,
                        cols=4,
                    ),
                    FieldSpec(
                        key="animegarden_request_interval",
                        label="请求间隔",
                        type="number",
                        default=1.0,
                        min=0.2,
                        max=10,
                        step=0.1,
                        suffix="秒",
                        cols=4,
                    ),
                    FieldSpec(
                        key="animegarden_timeout",
                        label="搜索超时",
                        type="number",
                        default=60,
                        min=5,
                        max=120,
                        suffix="秒",
                        cols=4,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        base_url = str(
            cls.config_value(config, "animegarden_base_url", "https://animes.garden/")
            or "https://animes.garden/"
        ).strip()
        timeout = int(cls.config_value(config, "animegarden_timeout", 60) or 60)
        interval = float(cls.config_value(config, "animegarden_request_interval", 1.0) or 1.0)
        return AnimeGardenClient(
            base_url=base_url,
            timeout=timeout,
            interval=interval,
            proxy=ctx.get("proxy"),
        )

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        limit = int(cls.config_value(config, "animegarden_result_limit", 10) or 10)
        return create_animegarden_provider(
            AnimeGardenSearchService(client, result_limit=limit),
            {"base_url": client.base_url, "limit": limit},
        )

