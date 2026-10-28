"""历史演示数据的人名脱敏映射。

种子数据已经统一改为张三、李四等占位名；早期账本流水是不可修改的
（wallet_transaction 只追加），所以历史里的真人名只在读取时脱敏，
绝不 UPDATE 账本。新增数据直接用占位名，不再经过这里。
"""

from __future__ import annotations

LEGACY_NAME_MAP = {
    "管理员-老王": "张三",
    "风控-张敏": "王五",
    "运营-陈磊": "钱七",
    "李彬源": "李四",
    "刘玉玲": "赵六",
    "刘鉴涛": "陈十四",
    "吴学根": "褚十五",
    "陆剑尘": "蒋十七",
    "李佩贤": "沈十八",
    "江斌": "孙八",
    "张伟": "周九",
    "李娜": "吴十",
    "王强": "郑十一",
    "陈静": "王十二",
    "刘洋": "冯十三",
    "汪静": "卫十六",
    "老王": "张三",
    "张敏": "王五",
    "陈磊": "钱七",
}


def display_name(value: str | None) -> str | None:
    """整段显示名替换（账号、处理人、操作人）。"""
    if not value:
        return value
    return LEGACY_NAME_MAP.get(value, value)


def display_text(value: str | None) -> str | None:
    """备注/回执等自由文本里的历史人名替换。"""
    if not value:
        return value
    out = value
    for old, new in LEGACY_NAME_MAP.items():
        out = out.replace(old, new)
    return out


def name_variants(value: str) -> list[str]:
    """筛选时同时匹配展示名和历史真人名，避免改名后搜不到旧流水。"""
    value = value.strip()
    if not value:
        return []
    variants = [value]
    variants += [old for old, new in LEGACY_NAME_MAP.items() if new == value and old not in variants]
    return variants
