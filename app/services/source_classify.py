"""按目录/文件名给「无番号」文件猜来源类别（待确认页分组用）。

不是番号解析器：只在没有可信 code_guess 时提供来源标签，避免所有无番号文件
被塞进同一个分组。规则基于常见片库目录命名，宁可标成「其他」也不误判。

类别：
- cosplayjav  CosplayJAV 自有内容
- fc2         FC2-PPV
- dorcel      欧美 Marc Dorcel（啄木鸟）
- blacked     欧美 Blacked
- vixen       欧美 Vixen
- tushy       欧美 Tushy
- brazzers    欧美 Brazzers
- naughty     欧美 Naughty America
- western     其它欧美厂牌（目录带 欧美/western/studio 等）
- amateur     素人/无码合集
- other       其它无番号
"""
from __future__ import annotations

import re

__all__ = ["detect_source", "SOURCE_LABELS"]

SOURCE_LABELS = {
    "cosplayjav": "CosplayJAV",
    "fc2": "FC2-PPV",
    "dorcel": "欧美 · Marc Dorcel（啄木鸟）",
    "blacked": "欧美 · Blacked",
    "vixen": "欧美 · Vixen",
    "tushy": "欧美 · Tushy",
    "brazzers": "欧美 · Brazzers",
    "naughty": "欧美 · Naughty America",
    "western": "欧美 · 其他厂牌",
    "mukousha": "蚊香社无码流出",
    "amateur": "素人/无码",
    "other": "其他无番号",
}

# 顺序：越具体的厂牌越先匹配
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cosplayjav", re.compile(r"cosplay[\s._-]*jav|cosplayjav")),
    ("fc2", re.compile(r"(^|[/_.\s-])fc2([/_.\s-]|$)|fc2[\s._-]?ppv")),
    ("dorcel", re.compile(r"dorcel|啄木鸟|marc[\s._-]*dorcel")),
    ("blacked", re.compile(r"blacked(?:raw)?")),
    ("vixen", re.compile(r"vixen(?:plus)?")),
    ("tushy", re.compile(r"tushy(?:raw)?")),
    ("brazzers", re.compile(r"brazzers|\bbz\b")),
    ("naughty", re.compile(r"naughty[\s._-]?america")),
    ("western", re.compile(r"欧美|western|euro[\s._-]?adult")),
    ("mukousha", re.compile(r"蚊香社|mukousha")),
    ("amateur", re.compile(r"素人|amateur|无码|無碼|uncensored")),
)


def detect_source(rel_path: str | None) -> tuple[str, str]:
    """返回 ``(key, label)``；识别不了时是 ``("other", "其他无番号")``。"""
    text = (rel_path or "").replace("\\", "/").lower()
    for key, pattern in _RULES:
        if pattern.search(text):
            return key, SOURCE_LABELS[key]
    return "other", SOURCE_LABELS["other"]
