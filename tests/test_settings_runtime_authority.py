from __future__ import annotations

from pathlib import Path

import yaml

from character_memory.config import Settings
from character_memory.settings_store import (
    LEGACY_SECRET_FIELDS,
    SETTINGS_SCHEMA,
    resolved_schema,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_EXAMPLE = ROOT / "config.example.yaml"


def test_settings_ui_validation_bounds_are_runtime_owned():
    """Presentation metadata must not duplicate Pydantic numeric limits."""

    # Presentation metadata may still contain legacy min/max while older
    # branches are being migrated, but the served schema must ignore/override
    # them from the Pydantic runtime contract.
    properties = Settings.model_json_schema()["properties"]
    for section in resolved_schema():
        for field in section["fields"]:
            if field.get("storage") == "env":
                continue
            prop = properties[field["name"]]
            candidates = (
                prop.get("anyOf")
                if isinstance(prop.get("anyOf"), list)
                else [prop]
            )
            scalar = next(
                (
                    item
                    for item in candidates
                    if item.get("type")
                    in {"boolean", "integer", "number", "string", "array"}
                ),
                prop,
            )
            assert field["runtime_type"] == scalar.get("type")
            assert field.get("min") == scalar.get("minimum")
            assert field.get("max") == scalar.get("maximum")


def test_config_example_values_match_runtime_defaults():
    """config.example is a checked reference, not a second defaults registry."""

    payload = yaml.safe_load(CONFIG_EXAMPLE.read_text(encoding="utf-8")) or {}
    defaults = Settings().model_dump()
    expected = set(Settings.model_fields) - set(LEGACY_SECRET_FIELDS)

    mismatches = {}
    for name in sorted(expected):
        actual = payload[name]
        wanted = defaults[name]
        # YAML keeps optional text overrides easy to edit with "", while the
        # runtime default uses None to mean "reuse the primary setting".
        if wanted is None and actual == "":
            continue
        if actual != wanted:
            mismatches[name] = {
                "example": actual,
                "runtime_default": wanted,
            }

    assert mismatches == {}
