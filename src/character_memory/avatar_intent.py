from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AvatarSearchIntent(BaseModel):
    """Small, safe planning result used to turn character context into image-search queries."""

    visual_intent: str = Field(min_length=1, max_length=300)
    # Providers may occasionally return more than requested. Normalize down to
    # three in the validator instead of rejecting an otherwise useful plan.
    queries: list[str] = Field(min_length=1)
    preferred_mood: str = Field(default="", max_length=100)
    preferred_style: str = Field(default="", max_length=160)

    @field_validator("queries")
    @classmethod
    def normalize_queries(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in value:
            query = " ".join(str(raw or "").split()).strip()[:180]
            if not query:
                continue
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(query)
            if len(result) >= 3:
                break
        if not result:
            raise ValueError("avatar search intent requires at least one non-empty query")
        return result


class AvatarIntentPlanner:
    """Ask the existing character model what avatar fits the character right now.

    Planning remains outside PersonRuntime: the result is ephemeral tool context and
    is never admitted into character memory. Only the short generated search query
    leaves the model boundary and reaches the image-search provider.
    """

    def __init__(self, model):
        self.model = model

    @staticmethod
    def _compact_text(value: Any, limit: int) -> str:
        if value is None:
            return ""
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, (dict, list, tuple)):
            try:
                text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                text = str(value)
        else:
            text = str(value)
        text = text.strip()
        return text[:limit]

    def plan(
        self,
        character_id: str,
        *,
        persona: str,
        mental_state: Any = None,
        recent_dialogue: list[str] | None = None,
        user_hint: str = "",
    ) -> AvatarSearchIntent:
        persona_text = self._compact_text(persona, 6000)
        mental_text = self._compact_text(mental_state, 1600) or "（暂无明确持续状态）"
        dialogue_lines = []
        for item in recent_dialogue or []:
            text = " ".join(str(item or "").split()).strip()
            if text:
                dialogue_lines.append(text[:360])
            if len(dialogue_lines) >= 8:
                break
        dialogue_text = "\n".join(dialogue_lines) or "（暂无近期对话）"
        hint = " ".join(str(user_hint or "").split()).strip()[:300] or "（用户没有额外指定）"

        prompt = f"""你正在为一个持续存在的聊天角色决定“此时此刻最适合使用什么头像”。
这不是普通聊天回复，也不是生成图片提示词；你的输出会被用于图片搜索引擎。

目标：
1. 核心身份和人物辨识度优先，短暂情绪只能轻度影响头像，不要因为一句话就彻底换人格或造型。
2. 结合当前 Mental State 和最近互动，判断现在更适合怎样的表情、气质、构图与氛围。
3. 头像需要适合 32~64px 聊天界面：主体清晰，优先正脸/近景/半身，避免复杂大场景和横幅。
4. queries 必须是 1~3 条简短的图片搜索关键词，而不是长篇描述。尽量包含稳定的人物名/身份线索 + 当前合适的视觉线索。
5. 搜索词绝不能包含用户姓名、用户隐私、聊天原句、关系秘密、私人事件或长期记忆内容。最近对话只用于判断气质，不能原样泄露到搜索词。
6. 不要输出思维过程。visual_intent 只写一句可展示给用户看的简短结论；preferred_mood / preferred_style 也只写结果。
7. 默认选择适合作为普通聊天头像的非露骨、非色情形象。

Character ID: {character_id}

[Persona]
{persona_text}

[Current Mental State]
{mental_text}

[Recent Dialogue - only for mood inference, never quote into queries]
{dialogue_text}

[Optional User Preference]
{hint}

只返回符合 AvatarSearchIntent 的 JSON：visual_intent、queries、preferred_mood、preferred_style。"""

        # OpenAICompatibleModel already exposes a generic structured-call path
        # through structured_with_images_for_session. Passing no images keeps the
        # normal text model while avoiding a second LLM client/agent architecture.
        method = getattr(self.model, "structured_for_session", None)
        if callable(method):
            result = method(prompt, AvatarSearchIntent, f"avatar-intent:{character_id}")
        else:
            method = getattr(self.model, "structured_with_images_for_session", None)
            if not callable(method):
                raise RuntimeError("loaded model does not support structured avatar planning")
            result = method(prompt, [], AvatarSearchIntent, f"avatar-intent:{character_id}")
        if isinstance(result, AvatarSearchIntent):
            return result
        return AvatarSearchIntent.model_validate(result)
