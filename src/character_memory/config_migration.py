from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import shutil

import yaml

CURRENT_CONFIG_VERSION = 1
_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s.*)?$")


@dataclass(frozen=True)
class ConfigMigrationResult:
    config_path: str
    backup_path: str | None
    changed: bool
    created: bool
    old_version: int
    new_version: int
    added_keys: tuple[str, ...]
    preserved_unknown_keys: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _load_mapping(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return dict(value)


def _config_version(data: dict) -> int:
    raw = data.get("config_version", 0)
    if raw in (None, ""):
        return 0
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config_version must be an integer, got {raw!r}") from exc
    if version < 0:
        raise ValueError(f"config_version must be >= 0, got {version}")
    return version


def _migrate_v0_to_v1(data: dict) -> dict:
    return dict(data)


_MIGRATIONS = {0: _migrate_v0_to_v1}


def _apply_version_migrations(data: dict, old_version: int) -> dict:
    if old_version > CURRENT_CONFIG_VERSION:
        raise ValueError(
            f"config version {old_version} is newer than this application supports "
            f"({CURRENT_CONFIG_VERSION}); update the application instead of downgrading the config"
        )
    migrated = dict(data)
    version = old_version
    while version < CURRENT_CONFIG_VERSION:
        migrate = _MIGRATIONS.get(version)
        if migrate is None:
            raise ValueError(f"no config migration registered for version {version} -> {version + 1}")
        migrated = migrate(migrated)
        version += 1
        migrated["config_version"] = version
    return migrated


def _yaml_inline(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _template_key_order(template_text: str) -> list[str]:
    keys: list[str] = []
    for line in template_text.splitlines():
        if line[:1].isspace() or line.lstrip().startswith("#"):
            continue
        match = _KEY_LINE.match(line)
        if match:
            keys.append(match.group(1))
    return keys


def _render_from_template(template_text: str, merged: dict, unknown_keys: list[str]) -> str:
    rendered: list[str] = []
    seen: set[str] = set()
    for line in template_text.splitlines():
        if line[:1].isspace() or line.lstrip().startswith("#"):
            rendered.append(line)
            continue
        match = _KEY_LINE.match(line)
        if not match:
            rendered.append(line)
            continue
        key = match.group(1)
        if key not in merged:
            rendered.append(line)
            continue
        comment = ""
        comment_index = line.find(" #")
        if comment_index >= 0:
            comment = line[comment_index:]
        rendered.append(f"{key}: {_yaml_inline(merged[key])}{comment}")
        seen.add(key)

    for key in merged:
        if key not in seen and key not in unknown_keys:
            rendered.append(f"{key}: {_yaml_inline(merged[key])}")

    if unknown_keys:
        rendered.extend([
            "",
            "# Preserved local/custom keys not present in the current template.",
            "# They are kept to avoid destructive migrations and may be ignored by the runtime.",
        ])
        for key in unknown_keys:
            rendered.append(f"{key}: {_yaml_inline(merged[key])}")
    return "\n".join(rendered).rstrip() + "\n"


def config_needs_migration(config_path: str | Path = "config.yaml", *, template_path: str | Path = "config.example.yaml") -> bool:
    config = Path(config_path)
    template = Path(template_path)
    if not config.exists():
        return False
    if not template.is_file():
        raise FileNotFoundError(f"config template not found: {template}")
    current = _load_mapping(config)
    template_data = _load_mapping(template)
    old_version = _config_version(current)
    if old_version > CURRENT_CONFIG_VERSION:
        raise ValueError(
            f"config version {old_version} is newer than this application supports ({CURRENT_CONFIG_VERSION})"
        )
    if old_version < CURRENT_CONFIG_VERSION:
        return True
    return any(key not in current for key in template_data)


def migrate_config(
    config_path: str | Path = "config.yaml",
    *,
    template_path: str | Path = "config.example.yaml",
    backup: bool = True,
    dry_run: bool = False,
) -> ConfigMigrationResult:
    config = Path(config_path)
    template = Path(template_path)
    if not template.is_file():
        raise FileNotFoundError(f"config template not found: {template}")

    template_text = template.read_text(encoding="utf-8")
    template_data = _load_mapping(template)
    template_order = _template_key_order(template_text)

    if not config.exists():
        created_text = template_text if template_text.endswith("\n") else template_text + "\n"
        parsed = yaml.safe_load(created_text) or {}
        if not isinstance(parsed, dict):
            raise ValueError(f"{template} must contain a YAML mapping at the top level")
        if not dry_run:
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(created_text, encoding="utf-8")
        return ConfigMigrationResult(
            config_path=str(config), backup_path=None, changed=True, created=True,
            old_version=0, new_version=_config_version(template_data) or CURRENT_CONFIG_VERSION,
            added_keys=tuple(template_order), preserved_unknown_keys=(),
        )

    original_text = config.read_text(encoding="utf-8")
    current = _load_mapping(config)
    old_version = _config_version(current)
    migrated_local = _apply_version_migrations(current, old_version)

    merged = dict(template_data)
    merged.update(migrated_local)
    merged["config_version"] = CURRENT_CONFIG_VERSION

    added_keys = [key for key in template_order if key not in current]
    unknown_keys = [key for key in current if key not in template_data]
    rendered = _render_from_template(template_text, merged, unknown_keys)
    parsed = yaml.safe_load(rendered) or {}
    if not isinstance(parsed, dict):
        raise ValueError("migrated config is not a YAML mapping")

    changed = rendered != original_text
    backup_path: Path | None = None
    if changed and not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        if backup:
            backup_path = Path(str(config) + ".bak")
            shutil.copyfile(config, backup_path)
        tmp = Path(str(config) + ".tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(config)

    return ConfigMigrationResult(
        config_path=str(config),
        backup_path=str(backup_path) if backup_path else None,
        changed=changed,
        created=False,
        old_version=old_version,
        new_version=CURRENT_CONFIG_VERSION,
        added_keys=tuple(added_keys),
        preserved_unknown_keys=tuple(unknown_keys),
    )


def migrate_config_if_needed(
    config_path: str | Path = "config.yaml",
    *,
    template_path: str | Path = "config.example.yaml",
) -> ConfigMigrationResult | None:
    if not config_needs_migration(config_path, template_path=template_path):
        return None
    return migrate_config(config_path, template_path=template_path, backup=True, dry_run=False)
