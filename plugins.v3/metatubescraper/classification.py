"""Stable, bounded library categories independent of file/version identity."""
import hashlib
import json
import re
import unicodedata


DEFAULT_MAKER_GROUPS = """S1=S1 NO.1 STYLE,エスワン,エスワン ナンバーワンスタイル
IDEA POCKET=IDEAPOCKET,アイデアポケット
MOODYZ=ムーディーズ
MADONNA=マドンナ
PREMIUM=プレミアム
ATTACKERS=アタッカーズ"""
OTHER_MAKER = '其他厂商'
UNKNOWN_MAKER = '未知厂商'


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).split()).casefold()


def directory_name(value):
    name = ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())
    if (not name or name in ('.', '..') or len(name.encode('utf-8')) > 255 or
            any(c in '/\\' or unicodedata.category(c).startswith('C') for c in name+str(value or ''))):
        raise ValueError('目录名不能为空、包含路径分隔符或控制字符，也不能为 . 或 ..')
    return name


def prefix_category(number):
    prefix = number.rsplit('-', 1)[0]
    return '数字番号' if prefix.isdigit() else prefix


class ClassificationPolicy:
    def __init__(self, layout='prefix', rules=None):
        if layout not in ('prefix', 'maker'):
            raise ValueError('媒体库分类必须为番号前缀或厂商归并')
        self.layout = layout
        self.rules = DEFAULT_MAKER_GROUPS if rules is None else str(rules)
        self.groups, self.aliases = [], {}
        seen = set()
        for line in self.rules.splitlines():
            if not line.strip():
                continue
            name, separator, aliases = line.partition('=')
            if not separator:
                raise ValueError('厂商规则每行需要使用 目录名=别名1,别名2')
            name = directory_name(name)
            key = normalized(name)
            if key in seen or key in (normalized(OTHER_MAKER), normalized(UNKNOWN_MAKER)):
                raise ValueError('厂商分组重名或使用了保留目录名：'+name)
            if re.fullmatch(r'[A-Z]{2,6}-\d{2,5}|FC2-PPV-\d+', name, re.I):
                raise ValueError('厂商目录名不能是完整番号：'+name)
            seen.add(key)
            self.groups.append(name)
            values = [name] + (re.split('[,，]', aliases) if separator else [])
            for alias in values:
                alias = normalized(alias)
                if not alias:
                    continue
                if alias in self.aliases:
                    raise ValueError('厂商别名重复归属：'+alias)
                self.aliases[alias] = name
        self.signature = hashlib.sha256(json.dumps(
            dict(layout=layout, aliases=self.aliases), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def category(self, number, metadata=None):
        if self.layout == 'prefix':
            return prefix_category(number)
        metadata = metadata or {}
        maker = normalized(metadata.get('maker')) or normalized(metadata.get('label'))
        return self.aliases.get(maker, OTHER_MAKER) if maker else UNKNOWN_MAKER

    def named_categories(self):
        return set(self.groups + [OTHER_MAKER, UNKNOWN_MAKER])

    def is_category(self, name, registered=()):
        return (name in self.named_categories() or name in registered or name == '数字番号' or
                bool(re.fullmatch(r'[A-Z]+(?:-[A-Z]+)*|\d{2,3}[A-Z]{2,6}', name)))

    def matches(self, name, number, registered=()):
        return name in self.named_categories() or name in registered or prefix_category(number) == name
