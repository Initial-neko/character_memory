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
from character_memory.envfile import delete_env_value, effective_env_value, env_source, parse_env_file, update_env_values, upsert_env_value
from character_memory.tts_registry import FORMAL_TTS_PROVIDERS


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

GSV_RUNTIME_FIELDS = {
    # The two shared base models plus the default template name. The reference
    # clip and its transcript moved into templates (voices/<name>.yaml), so the
    # per-field pair that used to live here is gone.
    "GSV_TTS_GPT_MODEL",
    "GSV_TTS_SOVITS_MODEL",
    "GSV_TTS_VOICE",
}
HOT_APPLY_FIELDS = {
    "tts_provider",
    "tts_voice",
    "tts_speed",
    *GSV_RUNTIME_FIELDS,
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
        "description": "正式实时聊天 TTS。Provider/Voice 由当前健康检查动态约束；未通过健康检查的 Provider 不可选择。Qwen3-TTS 不属于正式 Provider。TTS 选择保存后热生效，无需重启整个 stack。",
        "fields": [
            {
                "name": "tts_provider",
                "label": "TTS Provider",
                "type": "select",
                "options": [
                    {"value": item.id, "label": item.label}
                    for item in FORMAL_TTS_PROVIDERS
                ],
            },
            {
                "name": "tts_voice",
                "label": "TTS Voice",
                "type": "select",
                "options": [
                    {"value": voice, "label": f"{item.id} · {voice}"}
                    for item in FORMAL_TTS_PROVIDERS
                    for voice in item.voices
                ],
            },
            {"name": "tts_speed", "label": "TTS Speed", "type": "number", "min": 0.5, "max": 2, "step": 0.05},
            {
                "name": "tts_device",
                "label": "TTS Device (where supported)",
                "type": "select",
                "options": [
                    {"value": "cpu", "label": "CPU"},
                    {"value": "cuda", "label": "CUDA"},
                ],
            },
            {
                "name": "voice_silence_ms",
                "label": "Voice Call Pause (ms)",
                "type": "number",
                "min": 200,
                "max": 3000,
                "step": 50,
                "help": "语音通话里停顿多久算说完。调大=更容忍思考中的停顿，调小=接话更快。",
            },
        ],
    },
    {
        "id": "gsv-runtime",
        "title": "GSV-TTS-Lite Runtime",
        "description": "GSV 本地资产配置，持久化到项目 .env，不写入 config.yaml。两个底模 + 默认模板名；参考音频与参考文本由模板（voices/<名>.yaml）提供，在 TTS Lab 的声音合成页创建。保存后热配置 sidecar，无需重启整个 stack。",
        "fields": [
            {
                "name": "GSV_TTS_GPT_MODEL",
                "label": "GPT Model (.ckpt)",
                "type": "text",
                "storage": "env",
                "placeholder": "C:/path/to/Murasame-e15.ckpt",
            },
            {
                "name": "GSV_TTS_SOVITS_MODEL",
                "label": "SoVITS Model (.pth)",
                "type": "text",
                "storage": "env",
                "placeholder": "C:/path/to/Murasame_e8_s192.pth",
            },
            {
                # Options are injected per request by the Settings Center from the
                # live template registry (same graft the ``tts_voice`` field gets),
                # so the static schema carries the type but not the list.
                "name": "GSV_TTS_VOICE",
                "label": "Default Template",
                "type": "select",
                "storage": "env",
                "placeholder": "murasame",
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
        "id": "space-autonomy",
        "title": "Character Space",
        "description": "角色自主动态调度。默认每 24 小时一次 Opportunity；测试阶段可改成 1 小时或更短，一个角色一天因此可以发多条动态。到点只是让角色判断一次，仍然可以选择不发。",
        "fields": [
            {"name": "space_autonomy_enabled", "label": "Autonomous Space", "type": "checkbox"},
            {
                "name": "space_opportunity_interval_minutes",
                "label": "Opportunity Interval (min)",
                "type": "number",
                "min": 10,
                "max": 10080,
                "step": 10,
                "help": "同一角色两次正式 Space Opportunity 的最小间隔。1440=24H，60=1H，30=30min，10=10min。",
            },
            {
                "name": "space_max_posts_per_day",
                "label": "Max Posts / Day",
                "type": "number",
                "min": 0,
                "max": 200,
                "step": 1,
                "help": "每个角色每天最多发布几条自主动态。0 = 不限。它只是上限，不会强制发帖；角色判断不发时不消耗额度。",
            },
            {
                "name": "space_audience_size",
                "label": "Autonomous Audience",
                "type": "number",
                "min": 0,
                "max": 10,
                "step": 1,
                "help": "每条自主动态最多让多少个其他角色实际看到并判断是否互动；0 表示不自动分发。",
            },
            {
                "name": "space_scheduler_poll_seconds",
                "label": "Scheduler Poll (s)",
                "type": "number",
                "min": 10,
                "max": 3600,
                "step": 10,
                "help": "后台检查 next opportunity 是否到期的周期。只影响检查延迟，不改变 Opportunity Interval。",
            },
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

CONFIG_EDITABLE_FIELDS = {
    field["name"]
    for section in SETTINGS_SCHEMA
    for field in section["fields"]
    if field.get("storage") != "env"
}
ENV_EDITABLE_FIELDS = {
    field["name"]
    for section in SETTINGS_SCHEMA
    for field in section["fields"]
    if field.get("storage") == "env"
}
EDITABLE_FIELDS = CONFIG_EDITABLE_FIELDS | ENV_EDITABLE_FIELDS


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

        config_updates = {key: value for key, value in values.items() if key in CONFIG_EDITABLE_FIELDS}
        env_updates = {key: str(value or "").strip() for key, value in values.items() if key in ENV_EDITABLE_FIELDS}

        normalized_config: dict[str, Any] = {}
        if config_updates:
            current = load_settings(str(self.config_path)).model_dump()
            candidate = dict(current)
            candidate.update(config_updates)
            validated = Settings.model_validate(candidate)
            normalized_config = {key: getattr(validated, key) for key in config_updates}

        original_text = self._config_text()
        updated_text = _patch_flat_yaml(original_text, normalized_config) if normalized_config else original_text
        config_changed = updated_text != original_text

        env_before = parse_env_file(self.env_path)
        changed_env = {
            key: value
            for key, value in env_updates.items()
            if env_before.get(key, "") != value
        }

        if not config_changed and not changed_env:
            return {"changed": False, "backup": None, "updated": [], "restart_required": []}

        backup = self._write_backup(original_text) if config_changed and original_text else None
        env_existed = self.env_path.is_file()
        env_original = self.env_path.read_text(encoding="utf-8") if env_existed else ""
        try:
            if config_changed:
                _atomic_write(self.config_path, updated_text)
            if changed_env:
                update_env_values(self.env_path, changed_env)
        except Exception:
            # Keep the two persistence surfaces coherent. Each individual write
            # is atomic; this rollback prevents a failed second write from
            # leaving config.yaml and .env describing different logical saves.
            if config_changed:
                _atomic_write(self.config_path, original_text)
            if changed_env:
                if env_existed:
                    _atomic_write(self.env_path, env_original)
                else:
                    try:
                        self.env_path.unlink()
                    except FileNotFoundError:
                        pass
            raise

        updated = sorted([*normalized_config.keys(), *changed_env.keys()])
        restart_required = sorted(field for field in updated if field not in HOT_APPLY_FIELDS)
        return {
            "changed": True,
            "backup": str(backup) if backup else None,
            "updated": updated,
            "restart_required": restart_required,
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
        values = {name: getattr(settings, name) for name in CONFIG_EDITABLE_FIELDS}
        for name in ENV_EDITABLE_FIELDS:
            fallback = "murasame" if name == "GSV_TTS_VOICE" else ""
            values[name] = effective_env_value(name, self.env_path, fallback)
        return {
            "config_path": str(self.config_path),
            "env_path": str(self.env_path),
            "values": values,
            "schema": SETTINGS_SCHEMA,
            "secrets": self.secret_statuses(),
            "restart_policy": "Provider/Voice/Speed and GSV runtime assets hot-apply. GSV device hot-applies through its sidecar; Kokoro/Sherpa device changes require their runtime process to restart.",
            "last_migration": self.last_migration,
        }
