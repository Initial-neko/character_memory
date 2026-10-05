"""Optional presentation hints on the existing Person reaction, scoped to one call turn."""
from contextlib import contextmanager
from contextvars import ContextVar
import json

from pydantic import BaseModel, Field, field_validator

from character_memory.domain.models import ActionDecision, PersonReaction
from character_memory.live2d_web import model_capabilities


class Live2DPresentationRequest(BaseModel):
    token: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    character_id: str = Field(min_length=1, max_length=120)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class Live2DActionDecision(ActionDecision):
    live2d: dict | None = None

    @field_validator("live2d", mode="before")
    @classmethod
    def soften_optional_hint(cls, value):
        if not isinstance(value, dict):
            return None
        return {key: item for key, item in value.items() if key in {"motion", "expression"} and isinstance(item, str) and len(item) <= 64}


class Live2DPersonReaction(PersonReaction):
    actions: list[Live2DActionDecision] = Field(default_factory=list, max_length=3)
    action: Live2DActionDecision | None = None


_presentation = ContextVar("live2d_presentation", default=None)


def bind_presentation(root, request, allowed_character_ids):
    if root is None or request is None or request.character_id not in allowed_character_ids:
        return None
    capabilities = model_capabilities(root, request.character_id)
    if not capabilities or request.revision != capabilities["revision"]:
        return None
    return {**capabilities, "token": request.token, "character_id": request.character_id}


def event_presentation(event):
    value = event.metadata.get("live2d")
    if not isinstance(value, dict) or value.get("character_id") != event.character_id:
        return None
    return value


@contextmanager
def presentation_scope(event):
    token = _presentation.set(event_presentation(event))
    try:
        yield
    finally:
        _presentation.reset(token)


def presentation_schema():
    return Live2DPersonReaction if _presentation.get() else PersonReaction


def presentation_prompt(event):
    value = event_presentation(event)
    if not value:
        return ""
    resources = json.dumps({"motions": value["motions"], "expressions": value["expressions"]}, ensure_ascii=False)
    return ("\n# Live2D Presentation\n当前通话已开启 Live2D、自动动作和自动表情。"
            "可以在本轮已有文本 action 中附带 live2d={motion:动作组名,expression:表情名}，两项都可省略。"
            "只选下方真实资源，情绪符合人物和回答内容时才用；不要每句话都点头、不要编造动作，"
            "不要为了动画新增消息，不修改回复文本，不返回参数或 URL。普通待机由前端负责。\n"
            "Available Live2D Resources (names only): " + resources + "\n")


def behavior_metadata(event, action):
    capabilities = event_presentation(event)
    hint = getattr(action, "live2d", None)
    if not capabilities or not isinstance(hint, dict):
        return {}
    clean = {}
    for key, resource in (("motion", "motions"), ("expression", "expressions")):
        if isinstance(hint.get(key), str) and hint[key] in capabilities.get(resource, []):
            clean[key] = hint[key]
    if not clean:
        return {}
    return {"live2d": {**clean, **{key: capabilities[key] for key in ("token", "revision", "character_id")}}}
