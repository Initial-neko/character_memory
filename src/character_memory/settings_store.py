from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import yaml

from character_memory.config import Settings, load_settings
from character_memory.envfile import delete_env_value, env_source, parse_env_file, upsert_env_value


@dataclass(frozen=True)
class SecretSpec:
    name: str
    label: str
    legacy_field: str | None = None


SECRET_SPECS: tuple[SecretSpec, ...] = (
    SecretSpec("OPENCODE_GO_API_KEY", "LLM / OpenCode API Key", "api_key"),
    SecretSpec("EMBEDDING_API_KEY", "Embedding API Key", "embedding_api_key"),
    SecretSpec("SEARCHAPI_API_KEY", "SearchAPI API Key", "search_api_key"),
    SecretSpec("BRAVE_SEARCH_API_KEY", "Brave Search API Key", "search_api_key"),
    SecretSpec("AGNES_API_KEY", "Agnes Image API Key", "agnes_api_key"),
    SecretSpec("MSIMG_API_KEY", "ModelScope / msimg API Key", "msimg_api_key"),
    SecretSpec("HF_TOKEN", "Hugging Face Token", None),
)
SECRET_NAMES = {item.name for item in SECRET_SPECS}
LEGACY_SECRET_FIELDS = {
    item.legacy_field for item in SECRET_SPECS if item.legacy_field is not None
}


SETTINGS_SCHEMA: list[dict[str, Any]] = [
    {
        "id": "ai",
        "title": "AI / LLM",
        "description": "聊天模型与向量模型。V1 中保存后重启 stack 生效。",
        "fields": [
            {"name": "base_url", "label": "LLM Base URL", "type": "text"},
            {"name": "chat_model", "label": "Chat Model", "type": "text"},
            {"name": "vision_model", "label": "Vision Model", "type": "text", "placeholder": "留空时复用 Chat Model"},
            {"name": "chat_temperature", "label": "Temperature", "type": "number", "min": 0, "max": 2, "step": 0.05},
            {"name": "llm_attempts", "label": "LLM Attempts", "type": "number", "min": 1, "max": 4, "step": 1},
            {"name": "embedding_provider", "label": "Embedding Provider", "type": "text"},
            {"name": "embedding_model", "label": "Embedding Model", "type": "text"},
            {"name": "embedding_base_url", "label": "Embedding Base URL", "type": "text"},
        ],
    },
    {
        "id": "voice",
        "title": "Voice",
        "description": "正式聊天 TTS 默认设置。Provider Lab 仍用于试听。",
        "fields": [
            {
                "name": "tts_provider",
                "label": "TTS Provider",
                "type": "select",
                "options": [
                    {"value": "kokoro", "label": "Kokoro 82M v1.1 zh"},
                    {"value": "qwen3", "label": "Qwen3-TTS 0.6B"},
                    {"value": "edge", "label": "Microsoft Edge TTS (online)"},
                    {"value": "sherpa", "label": "Sherpa VITS"},
                ],
            },
            {
                "name": "tts_voice",
                "label": "Default Voice",
                "type": "select",
                "options": [
                    {"value": "zf_001", "label": "Kokoro zf_001"},
                    {"value": "zf_002", "label": "Kokoro zf_002"},
                    {"value": "zf_003", "label": "Kokoro zf_003"},
                    {"value": "zf_004", "label": "Kokoro zf_004"},
                    {"value": "Vivian", "label": "Qwen3 Vivian"},
                    {"value": "Serena", "label": "Qwen3 Serena"},
                    {"value": "Uncle_Fu", "label": "Qwen3 Uncle_Fu"},
                    {"value": "Dylan", "label": "Qwen3 Dylan"},
                    {"value": "Eric", "label": "Qwen3 Eric"},
                    {"value": "Ryan", "label": "Qwen3 Ryan"},
                    {"value": "Aiden", "label": "Qwen3 Aiden"},
                    {"value": "Ono_Anna", "label": "Qwen3 Ono_Anna"},
                    {"value": "Sohee", "label": "Qwen3 Sohee"},
                    {"value": "zh-CN-XiaoxiaoNeural", "label": "Edge 晓晓 Xiaoxiao"},
                    {"value": "zh-CN-XiaoyiNeural", "label": "Edge 晓伊 Xiaoyi"},
                    {"value": "zh-CN-YunjianNeural", "label": "Edge 云健 Yunjian"},
                    {"value": "zh-CN-YunxiNeural", "label": "Edge 云希 Yunxi"},
                    {"value": "zh-CN-YunyangNeural", "label": "Edge 云扬 Yunyang"},
                    {"value": "0", "label": "Sherpa speaker 0"},
                    {"value": "2", "label": "Sherpa speaker 2"},
                    {"value": "5", "label": "Sherpa speaker 5"},
                ],
            },
            {"name": "tts_speed", "label": "TTS Speed", "type": "number", "min": 0.5, "max": 2, "step": 0.05},
            {
                "name": "tts_device",
                "label": "TTS Device",
                "type": "select",
                "options": [
                    {"value": "cpu", "label": "CPU"},
                    {"value": "cuda", "label": "CUDA"},
                ],
            },
        ],
    },
    {
        "id": "visual",
        "title": "Search / ImageGen",
        "description": "图片搜索与生成 Provider。Key 在 Secrets 中维护。",
        "fields": [
            {
                "name": "search_provider",
                "label": "Search Provider",
                "type": "select",
                "options": [
                    {"value": "searchapi", "label": "SearchAPI"},
                    {"value": "brave", "label": "Brave"},
                ],
            },
            {"name": "search_country", "label": "Search Country", "type": "text"},
            {"name": "search_language", "label": "Search Language", "type": "text"},
            {
                "name": "search_safe_search",
                "label": "Safe Search",
                "type": "select",
                "options": [
                    {"value": "strict", "label": "strict"},
                    {"value": "moderate", "label": "moderate"},
                    {"value": "off", "label": "off"},
                ],
            },
            {
                "name": "image_generation_provider",
                "label": "ImageGen Provider",
                "type": "select",
                "options": [
                    {"value": "agnes", "label": "Agnes"},
                    {"value": "msimg", "label": "msimg / ModelScope"},
                ],
            },
            {"name": "image_generation_timeout_seconds", "label": "ImageGen Timeout (s)", "type": "number", "min": 10, "max": 600, "step": 5},
            {"name": "agnes_base_url", "label": "Agnes Base URL", "type": "text"},
            {"name": "agnes_image_model", "label": "Agnes Model", "type": "text"},
            {"name": "msimg_models", "label": "msimg Models", "type": "text"},
        ],
    },
    {
        "id": "behavior",
        "title": "Behavior / Memory",
        "description": "角色唤醒与召回参数。",
        "fields": [
            {"name": "recall_limit", "label": "Recall Limit", "type": "number", "min": 1, "max": 32, "step": 1},
            {"name": "proactive_wake_enabled", "label": "Proactive Wake", "type": "checkbox"},
            {"name": "proactive_wake_minutes", "label": "Wake Interval (min)", "type": "number", "min": 1, "max": 1440, "step": 1},
        ],
    },
    {
        "id": "storage",
        "title": "Storage",
        "description": "持久化路径。修改后必须重启，已有数据不会自动搬迁。",
        "fields": [
            {"name": "db_path", "label": "Database Path", "type": "text"},
            {"name": "media_dir", "label": "Media Directory", "type": "text"},
            {"name": "sticker_dir", "label": "Sticker Directory", "type": "text"},
            {"name": "avatar_dir", "label": "Avatar Directory", "type": "text"},
        ],
    },
]

EDITABLE_FIELDS = {
    field["name"]
    for section in SETTINGS_SCHEMA
    for field in section["fields"]
}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text if text.endswith("\n") else text + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _patch_flat_yaml(text: str, updates: dict[str, Any], *, remove: set[str] | None = None) -> str:
    remove = remove or set()
    lines = text.splitlines()
    remaining = dict(updates)
    output: list[str] = []
    top_level = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")
    for line in lines:
        match = top_level.match(line)
        if not match:
            output.append(line)
            continue
        key = match.group(1)
        if key in remove:
            continue
        if key in remaining:
            output.append(f"{key}: {_yaml_scalar(remaining.pop(key))}")
            continue
        output.append(line)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# Managed by Settings Center")
        for key, value in remaining.items():
            output.append(f"{key}: {_yaml_scalar(value)}")
    return "\n".join(output).rstrip() + "\n"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class SettingsStore:
    """Persistent settings editor for one flat Character Memory config file.

    V1 intentionally keeps the runtime config flat so existing callers remain
    compatible. The UI groups those fields into sections, while persistence
    preserves comments, unknown keys and the original file order.
    """

    def __init__(self, config_path: str = "config.yaml", env_path: str | None = None):
        self.config_path = Path(config_path)
        self.env_path = Path(env_path) if env_path else self.config_path.parent / ".env"
        self.last_migration: dict[str, Any] | None = None

    def _raw_config(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            return {}
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("config.yaml root must be a mapping")
        return data

    def _config_text(self) -> str:
        return self.config_path.read_text(encoding="utf-8") if self.config_path.is_file() else ""

    def _write_backup(self, text: str) -> Path:
        backup = self.config_path.with_name(f"{self.config_path.name}.bak.{_timestamp()}")
        suffix = 1
        while backup.exists():
            backup = self.config_path.with_name(f"{self.config_path.name}.bak.{_timestamp()}-{suffix}")
            suffix += 1
        _atomic_write(backup, text)
        return backup

    def migrate_legacy_secrets(self) -> dict[str, Any]:
        raw = self._raw_config()
        original_text = self._config_text()
        if not raw or not original_text:
            result = {"changed": False, "migrated": [], "backup": None}
            self.last_migration = result
            return result

        provider = str(raw.get("search_provider", "searchapi") or "searchapi").strip().lower()
        env_values = parse_env_file(self.env_path)
        migrated: list[dict[str, str]] = []
        remove_fields: set[str] = set()

        for spec in SECRET_SPECS:
            field = spec.legacy_field
            if not field or field not in raw:
                continue
            value = str(raw.get(field) or "").strip()
            if not value:
                remove_fields.add(field)
                continue
            if field == "search_api_key":
                target_name = "BRAVE_SEARCH_API_KEY" if provider == "brave" else "SEARCHAPI_API_KEY"
                if spec.name != target_name:
                    continue
            else:
                target_name = spec.name
            if target_name not in env_values:
                upsert_env_value(self.env_path, target_name, value)
                env_values[target_name] = value
            remove_fields.add(field)
            migrated.append({"field": field, "secret": target_name})

        if not remove_fields:
            result = {"changed": False, "migrated": [], "backup": None}
            self.last_migration = result
            return result

        sanitized_text = _patch_flat_yaml(original_text, {}, remove=remove_fields)
        # Never copy plaintext legacy secrets into a .bak. The migration backup is
        # deliberately the sanitized form; .env is the recovery source for keys.
        backup = self._write_backup(sanitized_text)
        _atomic_write(self.config_path, sanitized_text)
        result = {
            "changed": True,
            "migrated": migrated,
            "removed_fields": sorted(remove_fields),
            "backup": str(backup),
        }
        self.last_migration = result
        return result

    def save_values(self, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict) or not values:
            return {"changed": False, "backup": None, "updated": [], "restart_required": []}
        unknown = sorted(set(values) - EDITABLE_FIELDS)
        if unknown:
            raise ValueError(f"unsupported setting fields: {', '.join(unknown)}")

        # Validate against the complete effective model before writing anything.
        current = load_settings(str(self.config_path)).model_dump()
        candidate = dict(current)
        candidate.update(values)
        validated = Settings.model_validate(candidate)
        normalized = {key: getattr(validated, key) for key in values}

        original_text = self._config_text()
        updated_text = _patch_flat_yaml(original_text, normalized)
        if updated_text == original_text:
            return {"changed": False, "backup": None, "updated": [], "restart_required": []}

        backup = self._write_backup(original_text) if original_text else None
        _atomic_write(self.config_path, updated_text)
        updated = sorted(normalized)
        return {
            "changed": True,
            "backup": str(backup) if backup else None,
            "updated": updated,
            # V1 favors correctness over partial hot reload. Every process reads
            # config at startup, so all persisted changes are explicit restart work.
            "restart_required": updated,
        }

    def save_secret(self, name: str, value: str) -> dict[str, Any]:
        if name not in SECRET_NAMES:
            raise ValueError(f"unsupported secret: {name}")
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("secret value is empty")
        before = env_source(name, self.env_path)
        upsert_env_value(self.env_path, name, normalized)
        source = env_source(name, self.env_path)
        return {
            "name": name,
            "configured": True,
            "source": source,
            "system_override": before == "system" or source == "system",
            "restart_required": True,
        }

    def delete_secret(self, name: str) -> dict[str, Any]:
        if name not in SECRET_NAMES:
            raise ValueError(f"unsupported secret: {name}")
        removed = delete_env_value(self.env_path, name)
        source = env_source(name, self.env_path)
        return {
            "name": name,
            "removed_from_env": removed,
            "configured": source is not None,
            "source": source,
            "restart_required": True,
        }

    def secret_statuses(self) -> list[dict[str, Any]]:
        file_values = parse_env_file(self.env_path)
        statuses: list[dict[str, Any]] = []
        for spec in SECRET_SPECS:
            source = env_source(spec.name, self.env_path)
            configured = source is not None and bool(os.getenv(spec.name, file_values.get(spec.name, "")))
            statuses.append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "configured": configured,
                    "source": source,
                    "stored_in_env": spec.name in file_values,
                    "editable": True,
                    "value": None,
                }
            )
        return statuses

    def snapshot(self) -> dict[str, Any]:
        settings = load_settings(str(self.config_path))
        values = {name: getattr(settings, name) for name in EDITABLE_FIELDS}
        return {
            "config_path": str(self.config_path),
            "env_path": str(self.env_path),
            "values": values,
            "schema": SETTINGS_SCHEMA,
            "secrets": self.secret_statuses(),
            "restart_policy": "V1 saves persist immediately but running services must be restarted to consume changed config.",
            "last_migration": self.last_migration,
        }
