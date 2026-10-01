from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re

import yaml

from character_memory.config import Settings
from character_memory.settings_store import LEGACY_SECRET_FIELDS, SECRET_SPECS, SETTINGS_SCHEMA, resolved_schema


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"
CONFIG_EXAMPLE = ROOT / "config.example.yaml"
ENV_EXAMPLE = ROOT / ".env.example"


class _ControlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"button", "input", "select", "textarea"}:
            return
        values = dict(attrs or [])
        self.controls.append(
            {
                "tag": tag,
                "id": values.get("id", ""),
                "title": values.get("title", ""),
            }
        )


def test_config_example_lists_every_non_secret_settings_key_exactly_once():
    """A supported YAML knob must never exist only in source code.

    Secrets are the one deliberate exception: runtime compatibility fields still
    exist on Settings, but their persisted owner is .env and the example
    documents their environment names instead of encouraging plaintext YAML.
    """

    payload = yaml.safe_load(CONFIG_EXAMPLE.read_text(encoding="utf-8")) or {}
    assert isinstance(payload, dict)

    expected = set(Settings.model_fields) - set(LEGACY_SECRET_FIELDS)
    actual = set(payload)

    assert actual == expected, {
        "missing_from_example": sorted(expected - actual),
        "unknown_in_example": sorted(actual - expected),
    }


def test_config_example_values_match_runtime_defaults():
    """config.example is a checked reference, not a second defaults registry."""

    payload = yaml.safe_load(CONFIG_EXAMPLE.read_text(encoding="utf-8")) or {}
    defaults = Settings().model_dump()
    expected = set(Settings.model_fields) - set(LEGACY_SECRET_FIELDS)
    mismatches = {}
    for name in sorted(expected):
        actual = payload[name]
        wanted = defaults[name]
        # YAML uses an empty string for optional text fields so the copied file
        # remains easy to edit; runtime None and an empty optional override have
        # the same documented meaning.
        if wanted is None and actual == "":
            continue
        if actual != wanted:
            mismatches[name] = {"example": actual, "runtime_default": wanted}
    assert mismatches == {}


def test_settings_ui_validation_bounds_come_from_runtime_model():
    """Presentation metadata must not duplicate Pydantic numeric limits."""

    assert [
        field["name"]
        for section in SETTINGS_SCHEMA
        for field in section["fields"]
        if "min" in field or "max" in field
    ] == []

    properties = Settings.model_json_schema()["properties"]
    for section in resolved_schema():
        for field in section["fields"]:
            if field.get("storage") == "env":
                continue
            prop = properties[field["name"]]
            candidates = prop.get("anyOf") if isinstance(prop.get("anyOf"), list) else [prop]
            scalar = next(
                (item for item in candidates if item.get("type") in {"boolean", "integer", "number", "string", "array"}),
                prop,
            )
            assert field["runtime_type"] == scalar.get("type")
            assert field.get("min") == scalar.get("minimum")
            assert field.get("max") == scalar.get("maximum")

def test_retired_space_daily_window_keys_remain_loadable_but_are_not_supported_knobs(tmp_path):
    """Old config files stay readable without preserving no-op settings forever."""

    config = tmp_path / "config.yaml"
    config.write_text(
        "space_daily_window_start_hour: 18\n"
        "space_daily_window_end_hour: 22\n",
        encoding="utf-8",
    )

    from character_memory.config import load_settings

    settings = load_settings(str(config))
    assert "space_daily_window_start_hour" not in Settings.model_fields
    assert "space_daily_window_end_hour" not in Settings.model_fields
    assert not hasattr(settings, "space_daily_window_start_hour")
    assert not hasattr(settings, "space_daily_window_end_hour")


def test_every_config_example_key_has_an_explanation_comment():
    """The example is a reference manual, not merely a copyable value dump."""

    lines = CONFIG_EXAMPLE.read_text(encoding="utf-8").splitlines()
    top_level = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")
    missing: list[str] = []

    for index, line in enumerate(lines):
        match = top_level.match(line)
        if not match:
            continue
        if "#" in line:
            continue
        cursor = index - 1
        while cursor >= 0 and not lines[cursor].strip():
            cursor -= 1
        if cursor < 0 or not lines[cursor].lstrip().startswith("#"):
            missing.append(match.group(1))

    assert missing == []


def test_every_settings_field_and_secret_has_help_text():
    fields = [field for section in resolved_schema() for field in section["fields"]]
    assert fields
    assert [field["name"] for field in fields if not str(field.get("help") or "").strip()] == []
    assert [spec.name for spec in SECRET_SPECS if not spec.help.strip()] == []


def test_settings_ui_surfaces_help_for_generated_fields_secrets_and_actions():
    script = (WEB / "settings.js").read_text(encoding="utf-8")
    markup = (WEB / "settings.html").read_text(encoding="utf-8")

    # Dynamic schema/secret controls receive their server-supplied explanation.
    assert "input.title = field.help" in script
    assert "input.title = secret.help" in script

    # Dynamic action buttons explain whether they mutate .env or only run a
    # provider smoke test.
    assert 'save.title = "把输入的新 Secret' in script
    assert 'remove.title = "只删除项目 .env' in script
    assert 'previewButton.title = "使用当前 Settings' in script

    parser = _ControlParser()
    parser.feed(markup)
    parser.close()
    static = [item for item in parser.controls if item["id"]]
    assert static
    assert [item["id"] for item in static if not item["title"].strip()] == []


def test_every_dev_control_explains_its_scope_and_effect():
    """New Dev controls must say what they do before they can ship.

    Dev is a diagnostic surface, so this also guards against an unlabeled
    runtime override quietly becoming a second Settings Center.
    """

    parser = _ControlParser()
    parser.feed((WEB / "dev.html").read_text(encoding="utf-8"))
    parser.close()

    assert parser.controls
    missing = [
        item["id"] or f"<{item['tag']}>"
        for item in parser.controls
        if not item["title"].strip()
    ]
    assert missing == []

def test_env_example_keeps_gsv_voice_references_out_of_global_runtime_config():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "GSV_TTS_REF_AUDIO=" not in text
    assert "GSV_TTS_REF_TEXT=" not in text
    assert "voices/<name>.yaml" in text

