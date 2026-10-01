from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
import json
import os
from pathlib import Path
import re
import tempfile
import threading
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
    help: str = ""


SECRET_SPECS: tuple[SecretSpec, ...] = (
    SecretSpec(
        "OPENCODE_GO_API_KEY",
        "LLM / OpenCode API Key",
        "api_key",
        level="common",
        help="聊天、角色决策与视觉请求使用的 OpenAI-compatible API Key；只写入 .env，不会回显现有值。",
    ),
    SecretSpec(
        "EMBEDDING_API_KEY",
        "Embedding API Key",
        "embedding_api_key",
        level="common",
        help="仅远程 embedding provider 需要；本地 sentence-transformers 不使用该 Key。",
    ),
    SecretSpec(
        "SEARCHAPI_API_KEY",
        "SearchAPI API Key",
        "search_api_key",
        provider_field="search_provider",
        provider_value="searchapi",
        help="search_provider=searchapi 时用于图片发现与 World 公网搜索。",
    ),
    SecretSpec(
        "BRAVE_SEARCH_API_KEY",
        "Brave Search API Key",
        "search_api_key",
        provider_field="search_provider",
        provider_value="brave",
        help="search_provider=brave 时用于图片发现与 World 公网搜索。",
    ),
    SecretSpec(
        "AGNES_API_KEY",
        "Agnes Image API Key",
        "agnes_api_key",
        provider_field="image_generation_provider",
        provider_value="agnes",
        help="image_generation_provider=agnes 时使用；用于显式与自主 AI 生图。",
    ),
    SecretSpec(
        "MSIMG_API_KEY",
        "ModelScope / msimg API Key",
        "msimg_api_key",
        provider_field="image_generation_provider",
        provider_value="msimg",
        help="image_generation_provider=msimg 时使用；MODELSCOPE_API_TOKEN 仍作为兼容 fallback。",
    ),
    SecretSpec(
        "HF_TOKEN",
        "Hugging Face Token",
        None,
        level="diagnostic",
        help="可选，仅用于模型下载限流/鉴权；正常运行已下载模型时不需要。",
    ),
    SecretSpec(
        "METASO_MINIMAX_API_KEY",
        "MetaSo MiniMax H3 Video API Key",
        "metaso_minimax_api_key",
        provider_field="video_generation_provider",
        provider_value="metaso-minimax-h3",
        help="space_video_generation_enabled 且 video_generation_provider=metaso-minimax-h3 时使用。",
    ),
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


def _serialized_store_access(method):
    """Keep each read or mutation coherent across config.yaml and .env."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._mutation_lock:
            return method(self, *args, **kwargs)

    return guarded


# Settings are served in three levels. `common` is the short list a user has to
# decide for the stack to work at all and is the only one the first screen
# shows; `advanced` has a defensible default and is opened deliberately;
# `diagnostic` is transport/path/poll plumbing and is where troubleshooting
# starts. Advanced fields survive as a collapsed group in the page. Diagnostic
# fields remain in the API schema and config.example.yaml, but the normal UI
# deliberately leaves that backend plumbing out of its editable controls.
SETTING_LEVELS = ("common", "advanced", "diagnostic")
# A field that forgets its level must not land on the first screen. The page's
# contract is "common is the short list", so an unlabelled field is hidden
# rather than promoted: forgetting to declare a level should under-expose a
# field, never clutter the screen the user looks at first.
DEFAULT_SETTING_LEVEL = "diagnostic"

SETTING_HELP: dict[str, str] = {
    "base_url": "OpenAI-compatible LLM API 根地址。只有切换兼容服务或排查网络路由时才需要修改。",
    "chat_model": "主要文本模型 ID；Direct、Group、Space、World 等普通 LLM 调用默认使用它。",
    "vision_model": "视觉模型 ID。留空时复用 Chat Model；只有 Provider 要求独立视觉模型时才填写。",
    "chat_temperature": "普通聊天/角色生成的采样温度。越低越稳定，越高越发散；默认 0.7。",
    "llm_attempts": "一次逻辑 LLM 调用最多允许多少次真实请求，包括结构化输出修复/重试。",
    "embedding_provider": "记忆召回与语义相似度使用的向量后端；默认本地 sentence-transformers。",
    "embedding_model": "Embedding 模型 ID 或本地模型名；影响语义召回和去重向量。",
    "embedding_base_url": "远程 OpenAI-compatible embedding 服务地址。本地 embedding 时留空。",
    "tts_provider": "正式聊天/通话使用的 TTS Provider；只允许选择当前健康检查通过的正式 Provider。",
    "tts_voice": "当前 TTS Provider 的 Voice ID；角色没有专属 voice.yaml 时使用该默认声音。",
    "tts_speed": "正式 TTS 的语速倍率；1.0 为正常速度，是否支持由当前 Provider 决定。",
    "tts_device": "本地 TTS 推理设备。云 Provider 忽略；部分本地 Provider 修改后需要重启对应 Runtime。",
    "periodic_visual_observation_enabled": "用户在单聊通话中主动共享屏幕后，是否允许角色在画面显著变化时获得周期视觉观察机会；普通无变化画面不会调用 Vision。",
    "periodic_visual_observation_interval_seconds": "两次周期屏幕观察 Vision 调用之间的服务端最小间隔秒数；这是成本/节奏硬约束，不是浏览器轮询频率。",
    "periodic_visual_observation_max_per_hour": "每个单聊会话每小时最多接受多少次周期屏幕观察；0 = 禁用周期观察调用。",
    "voice_silence_ms": "语音通话中连续静音多久算一句话结束。大=更容忍思考停顿，小=更快接话。",
    "GSV_TTS_GPT_MODEL": "GSV-TTS-Lite 共享 GPT 模型 .ckpt 路径；保存到 .env，不写入 config.yaml。",
    "GSV_TTS_SOVITS_MODEL": "GSV-TTS-Lite 共享 SoVITS 模型 .pth 路径；保存到 .env，不写入 config.yaml。",
    "GSV_TTS_VOICE": "GSV 默认声音模板名，对应 voices/<name>.yaml；角色专属声音仍优先于默认模板。",
    "search_provider": "图片发现和 World 公网发现共用的搜索 Provider；不是私聊中的任意浏览器工具。",
    "search_country": "传给搜索 Provider 的地区提示，用于本地化搜索结果。",
    "search_language": "传给搜索 Provider 的语言提示，用于结果语言偏好。",
    "search_safe_search": "搜索安全过滤模式。strict 为保守默认值。",
    "web_browser_channel": "World Observation 使用的浏览器通道：auto / chromium / chrome。",
    "web_browser_timeout_seconds": "单个公网网页渲染/抓取允许的最长时间；只影响浏览器诊断与 World 抓取。",
    "web_browser_render_wait_ms": "页面 load 后额外等待 JavaScript 渲染正文的时间。",
    "image_generation_provider": "显式 AI 生图和角色自主生图使用的 Provider。",
    "image_generation_timeout_seconds": "单次 ImageGen Provider 请求的最大等待时间。",
    "agnes_base_url": "Agnes ImageGen 的 API 根地址；只有切换网关/排查路由时需要修改。",
    "agnes_image_model": "Agnes 使用的图像模型 ID。",
    "msimg_models": "msimg/ModelScope 模型别名列表，逗号分隔；多值时允许按实现进行 failover。",
    "recall_limit": "一次普通模型回合最多注入多少条召回记忆；越大上下文越多、Token 也越高。",
    "proactive_wake_enabled": "是否允许角色按周期获得主动醒来机会。醒来后仍可选择沉默。",
    "proactive_wake_minutes": "同一角色两次正常 Wake Opportunity 的间隔分钟数。",
    "proactive_dispatch_enabled": "是否派发已经到期的主动 Intent；关闭后保留 Intent 但不继续发出。",
    "proactive_min_dispatch_interval_minutes": "同一角色两次主动派发尝试的最小间隔；沉默也消耗该冷却。",
    "proactive_intent_min_delay_minutes": "新 Intent earliest_at 的最小延迟；0 表示不加额外延迟下限。",
    "proactive_max_pending_intents": "每个角色最多保留的 pending Intent 数量；0 = 不限。",
    "proactive_intent_dedup_enabled": "是否拒绝与近期 Intent 语义重复的新 Intent。",
    "proactive_intent_duplicate_similarity": "Intent 去重的余弦相似度阈值；越高越严格要求接近才判重复。",
    "proactive_intent_dedup_window_hours": "Intent 去重向前检查的时间窗口小时数。",
    "proactive_poll_seconds": "Intent 调度器检查是否到期的轮询延迟；不会改变 Intent 的真实间隔。",
    "space_autonomy_enabled": "是否允许角色获得自主 Space 发帖机会；Opportunity 不等于强制发布。",
    "space_opportunity_interval_minutes": "同一角色两次 Space Opportunity 的间隔；控制判断频率，不是发帖频率。",
    "space_max_posts_per_day": "每个角色每天允许发布的 Space 动态上限；0 = 不额外限制。",
    "space_media_enabled": "自主 Space 是否允许附带搜索图、生图或语音等媒体；关闭后仍可发纯文本。",
    "space_media_max_items": "单条自主 Space 动态实际执行的媒体数量上限；防止一次生成过多附件。",
    "space_image_search_enabled": "是否允许 Space 使用配置的搜索 Provider 找互联网图片。",
    "space_image_generation_enabled": "是否允许 Space 调用正式 ImageGen Provider 生成图片。",
    "space_world_observation_enabled": "是否允许一次 Space Opportunity 在表达前探索公开网页上下文。",
    "space_world_max_pages": "一次 Space World Observation 最多真正打开并读取的公网页面数量。",
    "space_world_max_chars_per_page": "每个 World 页面最多保留的可读正文字符数。",
    "space_audience_size": "新自主动态最多邀请多少其他角色判断点赞/评论；0 表示不自动分发。",
    "space_scheduler_poll_seconds": "Space Scheduler 检查 next opportunity 是否到期的轮询延迟。",
    "world_activity_enabled": "World Activity 总开关；控制 Pulse 与 Personal Browse 后台工作。",
    "world_pulse_enabled": "是否启用共享 World Pulse 聚合。",
    "world_pulse_sources": "World Pulse 读取的公网聚合/趋势页面，一行一个 URL。",
    "world_pulse_refresh_minutes": "自动刷新 World Pulse 的间隔分钟数。",
    "world_pulse_discussion_interval_minutes": "角色获得评论 Pulse Topic 机会的最小间隔。",
    "world_pulse_source_max_chars": "每个 Pulse 来源最多保留的可读正文字符数。",
    "world_pulse_max_topics": "一次 Pulse 刷新最多保留/暴露的主题数量。",
    "world_pulse_commenter_count": "一个 Pulse Topic 最多邀请多少角色独立判断是否评论。",
    "world_browse_enabled": "是否允许每个角色独立进行 Personal Browse。",
    "world_browse_interval_minutes": "同一角色两次 Personal Browse Opportunity 的间隔。",
    "world_browse_max_pages": "一次 Personal Browse 最多打开并读取的公网页面数。",
    "world_browse_daily_max": "每个角色每天最多执行多少次 Personal Browse；0 = 不限。",
    "world_activity_poll_seconds": "World Scheduler 检查 Pulse/Browse 是否到期的轮询延迟。",
    "group_autonomy_enabled": "是否允许已有群聊按周期获得自主交流机会。",
    "group_autonomy_interval_minutes": "同一群两次自主交流 Opportunity 的间隔。",
    "group_autonomy_max_messages": "一次自主群聊 Opportunity 最多生成多少条可见角色消息。",
    "group_autonomy_user_quiet_minutes": "用户刚发言后多少分钟内跳过正式自主群聊；0 = 关闭该保护。",
    "group_autonomy_poll_seconds": "自主群聊 Scheduler 的轮询延迟；不会改变 Opportunity 间隔。",
    "group_max_speakers_per_turn": "用户发一条群消息时最多让多少不同角色参与本轮回复。",
    "encounter_enabled": "是否允许 Random Encounter Scheduler 产生新的候选相遇。",
    "encounter_interval_minutes": "两次 Random Encounter Opportunity 的间隔。",
    "encounter_web_probability": "AUTO 邂逅选择公网资料来源的概率；其余概率走系统生成。",
    "encounter_max_pending": "最多保留多少个尚未处理的邂逅候选，防止候选无限堆积。",
    "encounter_poll_seconds": "Random Encounter Scheduler 检查是否到期的轮询延迟。",
    "db_path": "主 SQLite 数据库路径；Memory、调度、Usage 与大多数运行状态存放于此。",
    "media_dir": "普通媒体资产目录。留空时使用 <db parent>/media。",
    "sticker_dir": "表情包资产目录。留空时使用 <db parent>/stickers。",
    "avatar_dir": "当前角色头像资产目录。留空时使用 <db parent>/avatars。",
}


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
            item["help"] = str(item.get("help") or SETTING_HELP.get(item["name"], "")).strip()
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
        "title": "Visual / Search / ImageGen",
        "description": "屏幕视觉观察、图片搜索与生成 Provider。Key 在 Secrets 中维护。",
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
            {
                "name": "periodic_visual_observation_enabled",
                "label": "Periodic Screen Observation",
                "type": "checkbox",
                "level": "common",
            },
            {
                "name": "periodic_visual_observation_interval_seconds",
                "label": "Screen Observation Interval (s)",
                "type": "number",
                "min": 10,
                "max": 600,
                "step": 5,
                "level": "advanced",
            },
            {
                "name": "periodic_visual_observation_max_per_hour",
                "label": "Screen Observations / Hour",
                "type": "number",
                "min": 0,
                "max": 120,
                "step": 1,
                "level": "advanced",
            },
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
        "id": "proactive-dispatch",
        "title": "Proactive Intent",
        "description": "到期 Intent 的派发节奏。冷却、最小延迟和数量上限是服务端硬约束，不是建议值：模型每轮都可能再产出一条意图，没有这些约束时会形成每 30 秒一次的自我循环。",
        "fields": [
            {"name": "proactive_dispatch_enabled", "label": "Dispatch Due Intents", "type": "checkbox", "level": "advanced"},
            {
                "name": "proactive_min_dispatch_interval_minutes",
                "label": "Min Dispatch Interval (min)",
                "type": "number",
                "min": 1,
                "max": 1440,
                "step": 1,
                "level": "advanced",
                "help": "同一角色两次主动派发之间的最小间隔。沉默的轮次也会消耗它——那次 LLM 调用已经花掉了。",
            },
            {
                "name": "proactive_intent_min_delay_minutes",
                "label": "Intent Min Delay (min)",
                "type": "number",
                "min": 0,
                "max": 1440,
                "step": 1,
                "level": "advanced",
                "help": "新意图 earliest_at 的服务端下限。0 表示不设下限。",
            },
            {
                "name": "proactive_max_pending_intents",
                "label": "Max Pending Intents",
                "type": "number",
                "min": 0,
                "max": 200,
                "step": 1,
                "level": "advanced",
                "help": "每角色 PENDING 意图上限。0 表示不限制。",
            },
            {"name": "proactive_intent_dedup_enabled", "label": "Drop Duplicate Intents", "type": "checkbox", "level": "advanced"},
            {
                "name": "proactive_intent_duplicate_similarity",
                "label": "Duplicate Similarity",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "level": "diagnostic",
            },
            {
                "name": "proactive_intent_dedup_window_hours",
                "label": "Duplicate Window (h)",
                "type": "number",
                "min": 1,
                "max": 720,
                "step": 1,
                "level": "diagnostic",
            },
            {"name": "proactive_poll_seconds", "label": "Dispatch Poll (s)", "type": "number", "min": 10, "max": 3600, "step": 5, "level": "diagnostic"},
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
                "name": "space_video_generation_enabled",
                "label": "AI VideoGen",
                "type": "checkbox",
                "level": "advanced",
                "help": "付费能力，默认关闭。开启后角色仍只在动作/过程确实需要时间维度时选择视频，并受每日预算硬上限约束。",
            },
            {
                "name": "space_video_resolution",
                "label": "Video Resolution",
                "type": "select",
                "level": "advanced",
                "help": "传给服务商的分辨率。2K 单价更高，预算表按 768P / 2K 分别估算。",
                "options": [
                    {"value": "768P", "label": "768P"},
                    {"value": "2K", "label": "2K"},
                ],
            },
            {
                "name": "space_video_max_duration_seconds",
                "label": "Max Video Duration (s)",
                "type": "number",
                "min": 5,
                "max": 15,
                "step": 1,
                "level": "advanced",
                "help": "服务端硬上限；模型即使请求更长，也会被压到这里。",
            },
            {
                "name": "space_video_daily_budget_cny",
                "label": "Video Daily Budget (CNY)",
                "type": "number",
                "min": 0,
                "max": 10000,
                "step": 0.1,
                "level": "advanced",
                "help": "整个 Character Space 每个本地自然日的视频估算花费上限。0 = 禁止产生付费视频任务。",
            },
            {
                "name": "space_video_daily_max_generations",
                "label": "Video Daily Count Limit",
                "type": "number",
                "min": 0,
                "max": 100,
                "step": 1,
                "level": "advanced",
                "help": "每个本地自然日最多提交多少条视频任务。条数上限比金额上限更直观，且不会因为服务商改价而悄悄放松；0 = 禁止产生付费视频任务。",
            },
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
        # A save is a read/validate/patch/backup/write transaction, sometimes
        # spanning both config.yaml and .env. Atomic replacement protects one
        # file write, but without this lock two individually successful saves
        # can both patch the same old text and silently lose one update.
        self._mutation_lock = threading.RLock()

    @contextmanager
    def transaction(self):
        """Serialize a larger Settings operation that calls store methods."""

        with self._mutation_lock:
            yield

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

    @_serialized_store_access
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

    @_serialized_store_access
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

    @_serialized_store_access
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

    @_serialized_store_access
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
                    "help": spec.help,
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
