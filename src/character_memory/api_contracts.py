from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from character_memory.persona_builder import PersonaDraft


SOFT_ACTIVE_CHARACTERS = 10
MAX_ACTIVE_CHARACTERS = 20


class ChatImageRequest(BaseModel):
    filename: str = Field(default="image", min_length=1, max_length=180)
    data_url: str = Field(min_length=16)


class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=12000)
    sticker_id: str | None = Field(default=None, max_length=64)
    image: ChatImageRequest | None = None
    character_id: str = "rin"
    conversation_id: str = "default"
    at: datetime | None = None

    @model_validator(mode="after")
    def require_content(self):
        if not self.message.strip() and not (self.sticker_id or "").strip() and self.image is None:
            raise ValueError("message, sticker_id or image is required")
        if self.sticker_id and self.image is not None:
            raise ValueError("send a sticker or image in one user turn, not both")
        return self


class SimulateRequest(BaseModel):
    days: int = Field(default=1, ge=1, le=365)
    character_id: str = "rin"


class PersonaDraftRequest(BaseModel):
    description: str = Field(min_length=3, max_length=4000)
    name: str = Field(default="", max_length=48)
    age: int | None = Field(default=None, ge=1, le=120)
    tags: list[str] = Field(default_factory=list, max_length=8)


class CharacterCreationMetadataRequest(BaseModel):
    source: str = Field(default="PERSONA_BUILDER", min_length=1, max_length=64)
    prompt: str = Field(default="", max_length=12000)
    name_hint: str = Field(default="", max_length=120)
    age_hint: int | None = Field(default=None, ge=1, le=120)
    tags: list[str] = Field(default_factory=list, max_length=12)
    group_id: str | None = Field(default=None, max_length=120)


class CreateCharacterRequest(BaseModel):
    draft: PersonaDraft
    character_id: str = Field(default="", max_length=32)
    confirm_over_soft_limit: bool = False
    creation: CharacterCreationMetadataRequest | None = None


class CharacterCapacityConfirmationRequired(ValueError):
    def __init__(self, active_count: int, add_count: int):
        self.active_count = int(active_count)
        self.add_count = int(add_count)
        super().__init__(
            f"当前已有 {active_count} 位角色；继续新增会超过 {SOFT_ACTIVE_CHARACTERS} 位提醒阈值。"
        )

    def detail(self) -> dict:
        return {
            "code": "ACTIVE_CHARACTER_SOFT_LIMIT",
            "message": str(self),
            "active_count": self.active_count,
            "add_count": self.add_count,
            "soft_limit": SOFT_ACTIVE_CHARACTERS,
            "hard_limit": MAX_ACTIVE_CHARACTERS,
            "result_count": self.active_count + self.add_count,
            "confirmation_required": True,
        }


class CharacterCapacityExceeded(ValueError):
    def __init__(self, active_count: int, add_count: int):
        self.active_count = int(active_count)
        self.add_count = int(add_count)
        super().__init__(
            f"角色已达到容量上限：当前 {active_count} 位，本次新增 {add_count} 位，最多 {MAX_ACTIVE_CHARACTERS} 位。"
        )

    def detail(self) -> dict:
        return {
            "code": "ACTIVE_CHARACTER_HARD_LIMIT",
            "message": str(self),
            "active_count": self.active_count,
            "add_count": self.add_count,
            "soft_limit": SOFT_ACTIVE_CHARACTERS,
            "hard_limit": MAX_ACTIVE_CHARACTERS,
            "result_count": self.active_count + self.add_count,
            "confirmation_required": False,
        }
