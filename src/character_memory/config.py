from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Settings(BaseModel):
    api_key: str = ""
    base_url: str = "https://opencode.ai/zen/go/v1"
    chat_model: str = "deepseek-v4-flash"
    chat_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_attempts: int = Field(default=2, ge=1, le=4)

    embedding_provider: str = "sentence-transformers"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_api_key: str = ""
    embedding_base_url: str = ""

    db_path: str = "data/character-memory.db"
    persona_path: str = "personas/rin/persona.yaml"
    recall_limit: int = Field(default=8, ge=1, le=32)


def load_settings(path: str = "config.yaml") -> Settings:
    data: dict = {}
    p = Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data["api_key"] = os.getenv("OPENCODE_GO_API_KEY", data.get("api_key", ""))
    data["db_path"] = os.getenv("CHARACTER_MEMORY_DB_PATH", data.get("db_path", "data/character-memory.db"))
    return Settings.model_validate(data)


def load_persona(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")
