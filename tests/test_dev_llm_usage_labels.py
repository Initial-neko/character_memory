"""Dev Console LLM Usage 标签表必须覆盖每一个真实存在的 feature / purpose 枚举。

背景（issue #127）：``web/dev.js`` 用两张静态表把 ``llm_calls`` 里的枚举翻成中文。
问题是枚举的产生地有三处，而标签表只有一张：

1. ``llm/usage.py`` 里显式写下的 ``LlmUsageContext("FEATURE", "PURPOSE")`` 字面量；
2. ``runtime/reaction_engine.py`` 里通过 ``purpose = "..."`` 赋值（含行内三元）产生的
   base purpose；
3. 第 2 步的 ``_VISION`` 变体——``reaction_engine`` 在带图轮次会执行
   ``purpose = f"{purpose}_VISION"``。

第 3 类曾经整个漏掉，于是 Dev Console 直接显示英文（``DIRECT_REACTION_VISION``
而不是“私聊回复（带图）”）。本测试把这三处真实来源解析出来后，对 ``dev.js`` 的
标签表做子集断言：只要任一侧新增枚举而另一侧没跟上，CI 直接红。

这里刻意不去断言源码字符串的拼写，而是解析出枚举集合再比较——字符串断言会因为
表顺序调整、注释改动而误报。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 仓库根目录：tests/ 的上一级。
REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_JS = REPO_ROOT / "src" / "character_memory" / "web" / "dev.js"
USAGE_PY = REPO_ROOT / "src" / "character_memory" / "llm" / "usage.py"
REACTION_ENGINE = REPO_ROOT / "src" / "character_memory" / "runtime" / "reaction_engine.py"

_UPPER_LITERAL = re.compile(r'"([A-Z][A-Z0-9_]*)"')


def _read(path: Path) -> str:
    if not path.exists():
        pytest.fail(f"expected source file to exist: {path}")
    return path.read_text(encoding="utf-8")


def _label_keys(source: str, object_name: str) -> set[str]:
    """抽出 ``const NAME = Object.freeze({...})`` 里的 key 集合。"""
    match = re.search(
        rf"const\s+{object_name}\s*=\s*Object\.freeze\(\{{(.*?)\}}\)",
        source,
        flags=re.S,
    )
    assert match, f"{object_name} not found in dev.js"
    return set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*:", match.group(1), flags=re.M))


def _usage_context_pairs(source: str) -> set[tuple[str, str]]:
    """抽出 ``LlmUsageContext("FEATURE", "PURPOSE")`` 的成对字面量。"""
    return set(
        re.findall(r'LlmUsageContext\(\s*"([A-Z][A-Z0-9_]*)"\s*,\s*"([A-Z][A-Z0-9_]*)"', source)
    )


def _base_purposes(source: str) -> set[str]:
    """抽出 ``purpose = "..."`` 赋值行里的全部大写字面量。

    必须逐行扫描而不是匹配 ``^\s*purpose\s*=\s*"X"``，因为 ``reaction_engine``
    里存在行内三元：

        purpose = "GROUP_AUTONOMY" if event.event_type == EventType.TIME_TICK else "GROUP_REACTION"

    只取每行第一个字面量会静默丢抴 ``GROUP_REACTION``。
    """
    found: set[str] = set()
    for line in source.splitlines():
        if not re.search(r"\bpurpose\b[^=]*=", line):
            continue
        found |= set(_UPPER_LITERAL.findall(line))
    return found


def test_dev_js_label_tables_cover_every_usage_enum() -> None:
    dev_js = _read(DEV_JS)
    feature_keys = _label_keys(dev_js, "USAGE_FEATURE_LABELS")
    purpose_keys = _label_keys(dev_js, "USAGE_PURPOSE_LABELS")

    pairs = _usage_context_pairs(_read(USAGE_PY))
    base_purposes = _base_purposes(_read(REACTION_ENGINE))
    # 带图轮次会追加 ``_VISION`` 后缀；正则无法静态判断哪条分支真会带图，
    # 所以保守地把每个 base purpose 的 ``_VISION`` 兄弟都纳入要求。多一个
    # label 无害（用户看不到它），漏一个就会在面板上显示英文。
    vision_purposes = {f"{name}_VISION" for name in base_purposes}

    real_features = {feature for feature, _ in pairs}
    real_purposes = {purpose for _, purpose in pairs} | base_purposes | vision_purposes

    assert real_features, "failed to parse any LlmUsageContext feature"
    assert real_purposes, "failed to parse any real-purpose enum"

    missing_features = sorted(real_features - feature_keys)
    missing_purposes = sorted(real_purposes - purpose_keys)

    assert not missing_features, (
        "USAGE_FEATURE_LABELS 缺少这些 feature，Dev Console 会直接显示英文：\n  "
        + "\n  ".join(missing_features)
    )
    assert not missing_purposes, (
        "USAGE_PURPOSE_LABELS 缺少这些 purpose（含 _VISION 变体），Dev Console 会直接显示英文：\n  "
        + "\n  ".join(missing_purposes)
    )
