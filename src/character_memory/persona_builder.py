from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import re
import uuid

from pydantic import BaseModel, Field, ValidationError
import yaml

from character_memory.llm.usage import llm_usage_scope, new_logical_call_id


class PersonaDraft(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    age: int | None = Field(default=None, ge=1, le=120)
    identity: str = Field(min_length=1, max_length=240)
    tagline: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1600)
    personality: list[str] = Field(default_factory=list, max_length=8)
    conversation: str = Field(min_length=1, max_length=320)
    expression: str = Field(min_length=1, max_length=320)
    questions: str = Field(min_length=1, max_length=320)
    silence: str = Field(min_length=1, max_length=320)
    initiative: str = Field(min_length=1, max_length=320)
    disagreement: str = Field(min_length=1, max_length=320)
    care: str = Field(min_length=1, max_length=320)
    boundaries: list[str] = Field(default_factory=list, min_length=2, max_length=8)


_BLOCKED_TERMS = (
    "色情",
    "成人视频",
    "性爱",
    "性行为",
    "性癖",
    "裸聊",
    "擦边",
    "nsfw",
    "porn",
    "sexually explicit",
)


def ensure_safe_persona_text(text: str) -> None:
    lowered = text.casefold()
    if any(term.casefold() in lowered for term in _BLOCKED_TERMS):
        raise ValueError("人物创建不支持色情、擦边或性化设定")


def normalize_character_id(name: str, suggested: str = "") -> str:
    raw = (suggested or name).strip().lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")
    slug = re.sub(r"-{2,}", "-", slug)[:32]
    if slug:
        return slug
    return f"character-{uuid.uuid4().hex[:8]}"


def persona_root(persona_path: str) -> Path:
    configured = Path(persona_path)
    if configured.name == "persona.yaml" and configured.parent.parent != configured.parent:
        return configured.parent.parent
    return Path("personas")


def persona_document(draft: PersonaDraft, character_id: str) -> dict:
    identity = draft.identity.strip()
    if draft.age is not None and str(draft.age) not in identity:
        identity = f"{draft.age}岁，{identity}"
    return {
        "id": character_id,
        "name": draft.name.strip(),
        "identity": identity,
        "tagline": draft.tagline.strip(),
        "description": draft.description.strip(),
        "personality": [item.strip() for item in draft.personality if item.strip()],
        "behavior": {
            "conversation": draft.conversation.strip(),
            "expression": draft.expression.strip(),
            "questions": draft.questions.strip(),
            "silence": draft.silence.strip(),
            "initiative": draft.initiative.strip(),
            "disagreement": draft.disagreement.strip(),
            "care": draft.care.strip(),
        },
        "boundaries": [item.strip() for item in draft.boundaries if item.strip()],
    }


def dump_persona_yaml(draft: PersonaDraft, character_id: str) -> str:
    document = persona_document(draft, character_id)
    ensure_safe_persona_text(json.dumps(document, ensure_ascii=False))
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120)


def save_persona(persona_path: str, draft: PersonaDraft, character_id: str) -> Path:
    root = persona_root(persona_path)
    target = root / character_id / "persona.yaml"
    if target.exists():
        raise FileExistsError(f"character already exists: {character_id}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".yaml.tmp")
    temp.write_text(dump_persona_yaml(draft, character_id), encoding="utf-8")
    temp.replace(target)
    return target


def build_persona_prompt(description: str, *, name: str = "", age: int | None = None, tags: list[str] | None = None) -> str:
    ensure_safe_persona_text(description)
    tags = [tag.strip() for tag in (tags or []) if tag.strip()]
    hints = []
    if name.strip():
        hints.append(f"名字偏好：{name.strip()}")
    if age is not None:
        hints.append(f"年龄偏好：{age}")
    if tags:
        hints.append("辅助标签：" + "、".join(tags[:8]))
    hint_text = "\n".join(hints) or "没有额外硬性信息。"
    return f"""请根据用户描述生成一个长期聊天人物草稿。

用户描述：
{description.strip()}

补充信息：
{hint_text}

要求：
- 人物必须像独立的人，有自己的判断、兴趣、边界和不一致意见，不是客服，也不无条件迎合用户。
- 关系应该通过长期共同经历发展，不预设必须喜欢用户、依赖用户或迅速亲密。
- 表达方式要具体到真实聊天：句长、标点、emoji、追问、沉默、主动、分歧、关心方式都要有辨识度，但不要机械规则化。
- 可以有反差、怪癖、幽默感和鲜明兴趣，让长期聊天有可记忆的共同经历，但不要为了“留住用户”优化成瘾或 engagement。
- 不生成色情、擦边、性化内容；如果人物设定为未成年人，也不得包含任何性化元素。
- description 是给普通用户阅读的自然人物说明，不要写成系统提示词。
- personality 3~6 条；boundaries 2~5 条。
- 只返回 JSON，不要返回 Markdown。

JSON 字段必须包含：
name, age, identity, tagline, description, personality,
conversation, expression, questions, silence, initiative,
disagreement, care, boundaries。
"""


class PersonaBuilder:
    def __init__(self, model):
        self.model = model

    def generate(self, description: str, *, name: str = "", age: int | None = None, tags: list[str] | None = None) -> PersonaDraft:
        prompt = build_persona_prompt(description, name=name, age=age, tags=tags)
        system = (
            "你是 Character Memory 的人物设定生成器。你的任务是把自然语言愿望转换成连贯、可长期相处的人物草稿。"
            "保持人物自主性、边界和差异性。禁止色情、擦边、性化设定，也不要设计成迎合或制造依赖的留存机器。"
            "严格返回 JSON 对象。"
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        attempts = max(1, min(int(getattr(self.model, "attempts", 2)), 3))
        lock = getattr(self.model, "_call_lock", None)
        last_error: Exception | None = None
        logical_call_id = new_logical_call_id("persona")
        with llm_usage_scope(
            feature="PERSONA",
            purpose="PERSONA_BUILD",
            logical_call_id=logical_call_id,
        ):
            with (lock if lock is not None else nullcontext()):
                for attempt in range(attempts):
                    with llm_usage_scope(attempt=attempt + 1):
                        text = self.model._request(
                            messages,
                            conversation_id="persona-builder",
                            json_object=True,
                        )
                    try:
                        parser = getattr(self.model, "_json", json.loads)
                        draft = PersonaDraft.model_validate(parser(text))
                        ensure_safe_persona_text(json.dumps(draft.model_dump(mode="json"), ensure_ascii=False))
                        return draft
                    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                        last_error = exc
                        if attempt + 1 >= attempts:
                            break
                        messages.extend(
                            [
                                {"role": "assistant", "content": text},
                                {"role": "user", "content": "上一份 JSON 不符合人物草稿字段约束。保留人物含义，只修正 JSON 结构和字段；不要添加解释。"},
                            ]
                        )
        raise RuntimeError(f"persona draft invalid after {attempts} attempts: {last_error}") from last_error
