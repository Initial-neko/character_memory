from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import Clock, RealClock
from character_memory.config import Settings, load_persona, load_settings
from character_memory.life.runner import DayRunner
from character_memory.life.simulator import LifeSimulator
from character_memory.life.ticker import TimeTicker
from character_memory.llm.client import OpenAICompatibleModel
from character_memory.memory.embedding import DeterministicEmbedding, OpenAICompatibleEmbedding, SentenceTransformerEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


@dataclass
class AppBundle:
    settings: Settings
    store: SQLiteStore
    runtime: PersonRuntime
    chat: ChatService
    life: LifeSimulator
    ticker: TimeTicker
    days: DayRunner
    embeddings: object
    model: OpenAICompatibleModel
    clock: Clock

    def close(self) -> None:
        try:
            self.model.close()
        finally:
            self.store.close()


def build_embedding(settings: Settings):
    if settings.embedding_provider == "sentence-transformers":
        return SentenceTransformerEmbedding(settings.embedding_model)
    if settings.embedding_provider == "openai-compatible":
        key = settings.embedding_api_key or settings.api_key
        if not key or not settings.embedding_base_url:
            raise ValueError("openai-compatible embedding requires embedding_base_url and an API key")
        return OpenAICompatibleEmbedding(key, settings.embedding_model, settings.embedding_base_url)
    if settings.embedding_provider == "deterministic":
        return DeterministicEmbedding()
    raise ValueError(f"unknown embedding_provider: {settings.embedding_provider}")


def build_model(settings: Settings):
    if not settings.api_key:
        raise ValueError("Missing OPENCODE_GO_API_KEY or api_key in config.yaml")
    return OpenAICompatibleModel(settings.api_key, settings.chat_model, settings.base_url, temperature=settings.chat_temperature, attempts=settings.llm_attempts)


def build_app_from_settings(settings: Settings, *, clock: Clock | None = None) -> AppBundle:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(settings.db_path)
    embeddings = build_embedding(settings)
    model = build_model(settings)
    persona = load_persona(settings.persona_path)
    recall = VectorRecall(store, embeddings, limit=settings.recall_limit)
    runtime = PersonRuntime(store, recall, embeddings, model, persona)
    app_clock = clock or RealClock()
    chat = ChatService(store, runtime, app_clock)
    life = LifeSimulator(store, embeddings, model, persona, runtime)
    ticker = TimeTicker(store, runtime)
    days = DayRunner(store, life, ticker)
    return AppBundle(settings, store, runtime, chat, life, ticker, days, embeddings, model, app_clock)


def build_app(config_path: str = "config.yaml", *, clock: Clock | None = None) -> AppBundle:
    return build_app_from_settings(load_settings(config_path), clock=clock)
