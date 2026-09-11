from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def _rule_bodies(css: str, selector: str) -> list[str]:
    pattern = re.compile(re.escape(selector) + r"\s*\{([^}]*)\}", re.S)
    return pattern.findall(css)


def test_base_composer_remains_fixed_to_chat_viewport():
    base = (WEB / "styles.css").read_text(encoding="utf-8")
    rules = _rule_bodies(base, ".composer-wrap")
    assert rules, "base composer-wrap rule missing"
    assert any(re.search(r"position\s*:\s*fixed", body) for body in rules)
    assert any(re.search(r"left\s*:\s*280px", body) for body in rules)
    assert any(re.search(r"right\s*:\s*0", body) for body in rules)
    assert any(re.search(r"bottom\s*:\s*0", body) for body in rules)


def test_p0_16_does_not_override_composer_position_for_mentions():
    feature = (WEB / "p0_16.css").read_text(encoding="utf-8")
    composer_rules = _rule_bodies(feature, ".composer-wrap")
    assert all("position" not in body for body in composer_rules)

    mention_rules = _rule_bodies(feature, ".mention-menu")
    assert mention_rules, "mention menu style missing"
    assert any(re.search(r"position\s*:\s*absolute", body) for body in mention_rules)
    assert any("calc(50% - 390px)" in body for body in mention_rules)
