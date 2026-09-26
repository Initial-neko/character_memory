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
    # Level this key sits at while nothing selects it. Only the two keys the
    # stack cannot run without are ``common``; a provider key is ``advanced``
    # while its provider is selected and drops to ``diagnostic`` when it is not.
    level: str = "advanced"
    # The ``(settings field, value)`` pair that selects this key's provider, e.g.
    # ``("search_provider", "brave")``. Keys without a provider are always
    # relevant, so they keep ``level`` as given.
    provider_field: str | None = None
    provider_value: str | None = None


SECRET_SPECS: tuple[SecretSpec, ...] = (
    SecretSpec("OPENCODE_GO_API_KEY", "LLM / OpenCode API Key", "api_key", level="common"),
    SecretSpec("EMBEDDING_API_KEY", "Embedding API Key", "embedding_api_key", level="common"),
    SecretSpec(
        "SEARCHAPI_API_KEY",
        "SearchAPI API Key",
        "search_api_key",
        provider_field="search_provider",
        provider_value="searchapi",
    ),
    SecretSpec(
        "BRAVE_SEARCH_API_KEY",
        "Brave Search API Key",
        "search_api_key",
        provider_field="search_provider",
        provider_value="brave",
    ),
    SecretSpec(
        "AGNES_API_KEY",
        "Agnes Image API Key",
        "agnes_api_key",
        provider_field="image_generation_provider",
        provider_value="agnes",
    ),
    SecretSpec(
        "MSIMG_API_KEY",
        "ModelScope / msimg API Key",
        "msimg_api_key",
        provider_field="image_generation_provider",
        provider_value="msimg",
    ),
    SecretSpec("HF_TOKEN", "Hugging Face Token", None, level="diagnostic"),
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

# Settings are served in three levels. `common` is the short list a user has to
# decide for the stack to work at all and is the only one the first screen
# shows; `advanced` has a defensible default and is opened deliberately;
# `diagnostic` is transport/path/poll plumbing and is where troubleshooting
# starts. Every level survives as a real collapsed group in the page, so nothing
# is unreachable -- it is only out of the way.
SETTING_LEVELS = ("common", "advanced", "diagnostic")
# A field that forgets its level must not land on the first screen. The page's
# contract is "common is the short list", so an unlabelled field is hidden
# rather than promoted: forgetting to declare a level should under-expose a
# field, never clutter the screen the user looks at first.
DEFAULT_SETTING_LEVEL = "diagnostic"


def field_level(field: dict[str, Any]) -> str:
    """The level of a schema field, defaulting to the hidden one."""

    level = str(field.get("level") or "").strip()
    return level if level in SETTING_LEVELS else DEFAULT_SETTING_LEVEL


def resolved_schema() -> list[dict[str, Any]]:
    """``SETTINGS_SCHEMA`` with each field's level and restart policy filled in.

    The page must not have to infer either. A field's level decides which
    collapsed group it renders into, and ``restart_required`` is what the
    per-field marker next to its label reads, so both are part of the served
    contract rather than something the browser guesses from field names.
    """

    schema: list[dict[str, Any]] = []
    for section in SETTINGS_SCHEMA:
        fields = []
        for field in section["fields"]:
            item = dict(field)
            item["level"] = field_level(field)
            item["restart_required"] = item["name"] not in HOT_APPLY_FIELDS
            fields.append(item)
        schema.append({**section, "fields": fields})
    return schema


SETTINGS_SCHEMA: list[dict[str, Any]] = [
    {
        "id": "ai",
        "title": "AI / LLM",
        "description": "聊天模型与向量模型。V1 中保存后重启 stack 生效。",
        "fields": [
            {"name": "base_url", "label": "LLM Base URL", "type": "text", "level": "diagnostic"},
            {"name": "chat_model", "label": "Chat Model", "type": "text", "level": "advanced"},
            {"name": "vision_model", "label": "Vision Model", "type": "text", "level": "advanced", "placeholder": "留空时复用 Chat Model"},
            {"name": "chat_temperature", "label": "Temperature", "type": "number", "min": 0, "max": 2, "step": 0.05, "level": "common"},
            {"name": "llm_attempts", "label": "LLM Attempts", "type": "number", "min": 1, "max": 4, "step": 1, "level": "diagnostic"},
            {"name": "embedding_provider", "label": "Embedding Provider", "type": "text", "level": "diagnostic"},
            {"name": "embedding_model", "label": "Embedding Model", "type": "text", "level": "diagnostic"},
            {"name": "embedding_base_url", "label": "Embedding Base URL", "type": "text", "level": "diagnostic"},
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
                "level": "common",
                "options": [
                    {"value": item.id, "label": item.label}
                    for item in FORMAL_TTS_PROVIDERS
                ],
            },
            {
                "name": "tts_voice",
                "label": "TTS Voice",
                "type": "select",
                "level": "common",
                "options": [
                    {"value": voice, "label": f"{item.id} · {voice}"}
                    for item in FORMAL_TTS_PROVIDERS
                    for voice in item.voices
                ],
            },
            {"name": "tts_speed", "label": "TTS Speed", "type": "number", "min": 0.5, "max": 2, "step": 0.05, "level": "common"},
            {
                "name": "tts_device",
                "label": "TTS Device (where supported)",
                "type": "select",
                # CPU/GPU selection is machine-specific, but it is still a
                # deliberate user choice for local TTS and is used by the existing
                # Voice settings workflow. Keep it in Advanced rather than hiding
                # it with backend transport/plumbing defaults.
                "level": "advanced",
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
                "level": "advanced",
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
                "level": "advanced",
                "placeholder": "C:/path/to/Murasame-e15.ckpt",
            },
            {
                "name": "GSV_TTS_SOVITS_MODEL",
                "label": "SoVITS Model (.pth)",
                "type": "text",
                "storage": "env",
                "level": "advanced",
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
                "level": "advanced",
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
                "level": "advanced",
                "options": [
                    {"value": "searchapi", "label": "SearchAPI"},
                    {"value": "brave", "label": "Brave"},
                ],
            },
            {"name": "search_country", "label": "Search Country", "type": "text", "level": "diagnostic"},
            {"name": "search_language", "label": "Search Language", "type": "text", "level": "diagnostic"},
            {
                "name": "search_safe_search",
                "label": "Safe Search",
                "type": "select",
                "level": "diagnostic",
                "options": [
                    {"value": "strict", "label": "strict"},
                    {"value": "moderate", "label": "moderate"},
                    {"value": "off", "label": "off"},
                ],
            },
            {
                "name": "web_browser_channel",
                "label": "World Browser",
                "type": "select",
                "level": "diagnostic",
                "options": [
                    {"value": "auto", "label": "Auto (Playwright Chromium → Chrome)"},
                    {"value": "chromium", "label": "Playwright Chromium"},
                    {"value": "chrome", "label": "Installed Chrome"},
                ],
                "help": "World Observation 真正打开网页时使用的无头浏览器。",
            },
            {"name": "web_browser_timeout_seconds", "label": "World Browser Timeout (s)", "type": "number", "min": 3, "max": 90, "step": 1, "level": "diagnostic"},
            {"name": "web_browser_render_wait_ms", "label": "JS Render Wait (ms)", "type": "number", "min": 0, "max": 5000, "step": 100, "level": "diagnostic"},
            {
                "name": "image_generation_provider",
                "label": "ImageGen Provider",
                "type": "select",
                "level": "advanced",
                "options": [
                    {"value": "agnes", "label": "Agnes"},
                    {"value": "msimg", "label": "msimg / ModelScope"},
                ],
            },
            {"name": "image_generation_timeout_seconds", "label": "ImageGen Timeout (s)", "type": "number", "min": 10, "max": 600, "step": 5, "level": "diagnostic"},
            {"name": "agnes_base_url", "label": "Agnes Base URL", "type": "text", "level": "diagnostic"},
            {"name": "agnes_image_model", "label": "Agnes Model", "type": "text", "level": "diagnostic"},
            {"name": "msimg_models", "label": "msimg Models", "type": "text", "level": "diagnostic"},
        ],
    },
    {
        "id": "behavior",
        "title": "Behavior / Memory",
        "description": "角色唤醒与召回参数。",
        "fields": [
            {"name": "recall_limit", "label": "Recall Limit", "type": "number", "min": 1, "max": 32, "step": 1, "level": "advanced"},
            {"name": "proactive_wake_enabled", "label": "Proactive Wake", "type": "checkbox", "level": "common"},
            {"name": "proactive_wake_minutes", "label": "Wake Interval (min)", "type": "number", "min": 1, "max": 1440, "step": 1, "level": "advanced"},
        ],
    },
    {
        "id": "space-autonomy",
        "title": "Character Space",
        "description": "角色自主动态调度。默认每 24 小时一次 Opportunity；测试阶段可改成 1 小时或更短，一个角色一天因此可以发多条动态。到点只是让角色判断一次，仍然可以选择不发。",
        "fields": [
            {"name": "space_autonomy_enabled", "label": "Autonomous Space", "type": "checkbox", "level": "common"},
            {
                "name": "space_opportunity_interval_minutes",
                "label": "Opportunity Interval (min)",
                "type": "number",
                "min": 10,
                "max": 10080,
                "step": 10,
                "level": "advanced",
                "help": "同一角色两次正式 Space Opportunity 的最小间隔。1440=24H，60=1H，30=30min，10=10min。",
            },
            {
                "name": "space_max_posts_per_day",
                "label": "Max Posts / Day",
                "type": "number",
                "min": 0,
                "max": 200,
                "step": 1,
                "level": "diagnostic",
                "help": "每个角色每天最多发布几条自主动态。0 = 不限。它只是上限，不会强制发帖；角色判断不发时不消耗额度。",
            },
            {
                "name": "space_media_enabled",
                "label": "Space Media",
                "type": "checkbox",
                "level": "common",
                "help": "允许角色在自主动态里自然选择图片；关闭后仍可正常发布纯文字动态。",
            },
            {
                "name": "space_media_max_items",
                "label": "Max Media / Post",
                "type": "number",
                "min": 0,
                "max": 9,
                "step": 1,
                "level": "diagnostic",
                "help": "单条自主动态最多执行多少张图片。0 = 禁用媒体；存储层硬上限仍为 9。",
            },
            {"name": "space_image_search_enabled", "label": "Web Image Search", "type": "checkbox", "level": "advanced"},
            {"name": "space_image_generation_enabled", "label": "AI ImageGen", "type": "checkbox", "level": "advanced"},
            {
                "name": "space_world_observation_enabled",
                "label": "World Observation",
                "type": "checkbox",
                "level": "advanced",
                "help": "允许角色在 Space Opportunity 中先决定是否探索公开互联网；搜索到内容不等于一定记忆或发动态。",
            },
            {
                "name": "space_world_max_pages",
                "label": "World Pages / Opportunity",
                "type": "number",
                "min": 1,
                "max": 4,
                "step": 1,
                "level": "diagnostic",
            },
            {
                "name": "space_world_max_chars_per_page",
                "label": "World Text / Page",
                "type": "number",
                "min": 500,
                "max": 16000,
                "step": 500,
                "level": "diagnostic",
            },
            {
                "name": "space_audience_size",
                "label": "Autonomous Audience",
                "type": "number",
                "min": 0,
                "max": 10,
                "step": 1,
                "level": "diagnostic",
                "help": "每条自主动态最多让多少个其他角色实际看到并判断是否互动；0 表示不自动分发。",
            },
            {
                "name": "space_scheduler_poll_seconds",
                "label": "Scheduler Poll (s)",
                "type": "number",
                "min": 10,
                "max": 3600,
                "step": 10,
                "level": "diagnostic",
                "help": "后台检查 next opportunity 是否到期的周期。只影响检查延迟，不改变 Opportunity Interval。",
            },
        ],
    },
    {
        "id": "world-activity",
        "title": "World Activity",
        "description": "角色上网观察与 Space 发帖解耦。这里保存 Pulse / Personal Browse 的正式调度参数；Dev Console 只负责手动验收。",
        "fields": [
            {"name": "world_activity_enabled", "label": "World Activity", "type": "checkbox", "level": "advanced"},
            {"name": "world_pulse_enabled", "label": "World Pulse", "type": "checkbox", "level": "advanced"},
            {
                "name": "world_pulse_sources",
                "label": "Pulse Sources",
                "type": "textarea-list",
                "level": "advanced",
                "help": "每行一个公开信息聚合/趋势页面 URL；最多 12 个。页面内容始终作为不可信外部数据。",
            },
            {
                "name": "world_pulse_refresh_minutes",
                "label": "Pulse Refresh (min)",
                "type": "number",
                "min": 10,
                "max": 10080,
                "step": 10,
                "level": "advanced",
            },
            {
                "name": "world_pulse_discussion_interval_minutes",
                "label": "Pulse Discussion (min)",
                "type": "number",
                "min": 10,
                "max": 10080,
                "step": 10,
                "level": "advanced",
            },
            {
                "name": "world_pulse_max_topics",
                "label": "Max Pulse Topics",
                "type": "number",
                "min": 1,
                "max": 12,
                "step": 1,
                "level": "advanced",
            },
            {
                "name": "world_pulse_commenter_count",
                "label": "Pulse Commenter Candidates",
                "type": "number",
                "min": 0,
                "max": 10,
                "step": 1,
                "level": "advanced",
                "help": "只是候选人数；每个角色仍可独立判断保持沉默。",
            },
            {"name": "world_browse_enabled", "label": "Personal Browse", "type": "checkbox", "level": "advanced"},
            {
                "name": "world_browse_interval_minutes",
                "label": "Personal Browse Interval (min)",
                "type": "number",
                "min": 10,
                "max": 10080,
                "step": 10,
                "level": "advanced",
                "help": "每个活跃角色独立的上网机会基准间隔；当前基线 30 分钟。实际执行会带少量抖动，不等于每 30 分钟必定浏览。",
            },
            {
                "name": "world_browse_daily_max",
                "label": "Max Browses / Day",
                "type": "number",
                "min": 0,
                "max": 200,
                "step": 1,
                "level": "advanced",
                "help": "每个活跃角色每天最多浏览几次。每次浏览都要花掉一次付费搜索额度，而所有角色共用同一个 key，所以间隔本身不构成上限（30 分钟 = 48 次/天）。0 = 不限。它只是上限，不会强制浏览；未到期的角色不消耗额度。",
            },
            {
                "name": "world_pulse_source_max_chars",
                "label": "Pulse Text / Source",
                "type": "number",
                "min": 1000,
                "max": 20000,
                "step": 500,
                "level": "diagnostic",
            },
            {
                "name": "world_browse_max_pages",
                "label": "Browse Pages / Opportunity",
                "type": "number",
                "min": 1,
                "max": 4,
                "step": 1,
                "level": "diagnostic",
            },
            {
                "name": "world_activity_poll_seconds",
                "label": "World Scheduler Poll (s)",
                "type": "number",
                "min": 10,
                "max": 3600,
                "step": 10,
                "level": "diagnostic",
            },
        ],
    },
    {
        "id": "group-autonomy",
        "title": "Autonomous Group Chat",
        "description": "让已有群聊偶尔自己聊起来。每次 Opportunity 只允许一条很短的角色链；新用户消息始终优先，不会无限自循环。",
        "fields": [
            {"name": "group_autonomy_enabled", "label": "Autonomous Group Chat", "type": "checkbox", "level": "common"},
            {
                "name": "group_autonomy_interval_minutes",
                "label": "Opportunity Interval (min)",
                "type": "number",
                "min": 10,
                "max": 10080,
                "step": 10,
                "level": "advanced",
                "help": "同一群聊两次正式自主交流机会的间隔。默认 360=6H；测试可改 10/30/60。",
            },
            {
                "name": "group_autonomy_max_messages",
                "label": "Max Messages / Opportunity",
                "type": "number",
                "min": 1,
                "max": 4,
                "step": 1,
                "level": "diagnostic",
                "help": "一次自主机会最多提交多少条角色消息。每个角色最多说一次，避免群聊无限自循环。",
            },
            {
                "name": "group_autonomy_user_quiet_minutes",
                "label": "User Quiet Guard (min)",
                "type": "number",
                "min": 0,
                "max": 1440,
                "step": 5,
                "level": "diagnostic",
                "help": "用户刚在群里说过话时不抢话；0 = 关闭该保护。",
            },
            {
                "name": "group_autonomy_poll_seconds",
                "label": "Scheduler Poll (s)",
                "type": "number",
                "min": 10,
                "max": 3600,
                "step": 10,
                "level": "diagnostic",
                "help": "后台检查到期机会的周期，只影响检查延迟。",
            },
        ],
    },
    {
        "id": "group-conversation",
        "title": "Group Conversation",
        "description": "用户消息触发的一轮群聊最多让多少位成员接话。被 @ 的成员始终参与；未点名的成员从滚动顺序尾部截断，因此每轮听到的人仍然轮换。",
        "fields": [
            {
                "name": "group_max_speakers_per_turn",
                "label": "Max Speakers / Turn",
                "type": "number",
                "min": 1,
                "max": 12,
                "step": 1,
                "level": "advanced",
                "help": "默认 5。这是后端成本与噪音的形状旋钮，不改变持久化语义；设为 12 恢复“每个成员都被问到”的旧行为。",
            },
        ],
    },
    {
        "id": "encounter",
        "title": "Random Encounter",
        "description": "随机邂逅的正式调度配置。这里保存重启后仍生效的值；手动触发和临时验收留在 Dev Console。",
        "fields": [
            {"name": "encounter_enabled", "label": "Random Encounter", "type": "checkbox", "level": "advanced"},
            {
                "name": "encounter_interval_minutes",
                "label": "Encounter Interval (min)",
                "type": "number",
                "min": 10,
                "max": 10080,
                "step": 10,
                "level": "advanced",
                "help": "两次正式随机邂逅机会之间的间隔。1440=24H；Dev Console 可手动触发而不修改这里。",
            },
            {
                "name": "encounter_web_probability",
                "label": "Web Encounter Probability",
                "type": "number",
                "min": 0,
                "max": 1,
                "step": 0.05,
                "level": "advanced",
                "help": "AUTO 模式选择真实互联网资料来源的概率；剩余概率走系统生成。",
            },
            {
                "name": "encounter_max_pending",
                "label": "Max Pending Candidates",
                "type": "number",
                "min": 1,
                "max": 10,
                "step": 1,
                "level": "diagnostic",
                "help": "未处理候选达到上限后，Scheduler 暂停继续堆积新邂逅。",
            },
            {
                "name": "encounter_poll_seconds",
                "label": "Scheduler Poll (s)",
                "type": "number",
                "min": 10,
                "max": 3600,
                "step": 10,
                "level": "diagnostic",
                "help": "后台检查是否到期的周期，只影响检查延迟。",
            },
        ],
    },
    {
        "id": "storage",
        "title": "Storage",
        "description": "持久化路径。修改后必须重启，已有数据不会自动搬迁。",
        "fields": [
            {"name": "db_path", "label": "Database Path", "type": "text", "level": "diagnostic"},
            {"name": "media_dir", "label": "Media Directory", "type": "text", "level": "diagnostic"},
            {"name": "sticker_dir", "label": "Sticker Directory", "type": "text", "level": "diagnostic"},
            {"name": "avatar_dir", "label": "Avatar Directory", "type": "text", "level": "diagnostic"},
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
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
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


def secret_level(spec: SecretSpec, values: dict[str, Any]) -> str:
    """Where a key belongs right now, given what the config currently selects.

    Only the selected search/image-generation provider's key is ``advanced``;
    the other provider's key drops to ``diagnostic`` so the page does not offer
    two keys for a provider that is not in use. It stays reachable there on
    purpose: a key cannot be filled in *after* switching to the provider that
    needs it, so the collapsed group is the "fill this before you switch" slot.
    """

    if spec.provider_field and spec.provider_value:
        selected = str(values.get(spec.provider_field) or "").strip().lower()
        if selected != spec.provider_value:
            return "diagnostic"
    return spec.level


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

    def editable_values(self) -> dict[str, Any]:
        """The current value of every field this page may edit."""

        settings = load_settings(str(self.config_path))
        values = {name: getattr(settings, name) for name in CONFIG_EDITABLE_FIELDS}
        for name in ENV_EDITABLE_FIELDS:
            fallback = "murasame" if name == "GSV_TTS_VOICE" else ""
            values[name] = effective_env_value(name, self.env_path, fallback)
        return values

    def secret_statuses(self, values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        current = self.editable_values() if values is None else values
        file_values = parse_env_file(self.env_path)
        statuses: list[dict[str, Any]] = []
        for spec in SECRET_SPECS:
            source = env_source(spec.name, self.env_path)
            configured = source is not None and bool(os.getenv(spec.name, file_values.get(spec.name, "")))
            statuses.append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "level": secret_level(spec, current),
                    "configured": configured,
                    "source": source,
                    "stored_in_env": spec.name in file_values,
                    "editable": True,
                    "value": None,
                }
            )
        # Same level first, then the keys already in place: a configured key
        # answers "is this set up", and the ones that still need filling follow.
        # Stable, so the declared order breaks ties.
        level_rank = {level: index for index, level in enumerate(SETTING_LEVELS)}
        statuses.sort(key=lambda item: (level_rank.get(item["level"], 9), 0 if item["configured"] else 1))
        return statuses

    def snapshot(self) -> dict[str, Any]:
        values = self.editable_values()
        return {
            "config_path": str(self.config_path),
            "env_path": str(self.env_path),
            "values": values,
            "schema": resolved_schema(),
            "secrets": self.secret_statuses(values),
            "restart_policy": "Provider/Voice/Speed and GSV runtime assets hot-apply. GSV device hot-applies through its sidecar; Kokoro/Sherpa device changes require their runtime process to restart.",
            "last_migration": self.last_migration,
        }
