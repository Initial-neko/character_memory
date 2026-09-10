from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import time

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import Clock, RealClock
from character_memory.config import Settings, discover_character_profiles, load_persona, load_settings, resolve_sticker_dir
from character_memory.images import load_image_catalog
from character_memory.life.runner import DayRunner
from character_memory.life.simulator import LifeSimulator
from character_memory.life.ticker import TimeTicker
from character_memory.llm.client import OpenAICompatibleModel
from character_memory.memory.embedding import DeterministicEmbedding, OpenAICompatibleEmbedding, SentenceTransformerEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.stickers import load_global_sticker_catalog
from character_memory.storage.sqlite import SQLiteStore


logger = logging.getLogger("character_memory.app")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


@dataclass
class AppBundle:
    settings: Settings
    store: SQLiteStore
    runtime: PersonRuntime
    runtimes: dict[str, PersonRuntime]
    characters: list[dict[str, str]]
    chat: ChatService
    life: LifeSimulator
    ticker: TimeTicker
    days: DayRunner
    embeddings: object
    model: OpenAICompatibleModel
    clock: Clock
    init_timings: dict[str, float]

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
    return OpenAICompatibleModel(
        settings.api_key,
        settings.chat_model,
        settings.base_url,
        temperature=settings.chat_temperature,
        attempts=settings.llm_attempts,
        vision_model=settings.vision_model,
    )


def _default_character_id(settings: Settings, profiles: list[dict[str, str]]) -> str:
    configured = Path(settings.persona_path)
    for profile in profiles:
        if Path(profile["persona_path"]) == configured:
            return profile["id"]
    if any(profile["id"] == "rin" for profile in profiles):
        return "rin"
    return profiles[0]["id"]


def build_app_from_settings(settings: Settings, *, clock: Clock | None = None) -> AppBundle:
    if not settings.api_key:
        raise ValueError("Missing OPENCODE_GO_API_KEY or api_key in config.yaml")

    total = time.perf_counter()
    timings: dict[str, float] = {}
    logger.info("app.init start model=%s vision_model=%s embedding=%s/%s", settings.chat_model, settings.vision_model, settings.embedding_provider, settings.embedding_model)

    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    stage = time.perf_counter()
    store = SQLiteStore(settings.db_path)
    timings["store_ms"] = _ms(stage)
    try:
        stage = time.perf_counter()
        embeddings = build_embedding(settings)
        timings["embedding_load_ms"] = _ms(stage)
        logger.info("app.init embedding ready duration_ms=%.1f", timings["embedding_load_ms"])

        stage = time.perf_counter()
        model = build_model(settings)
        timings["model_init_ms"] = _ms(stage)

        stage = time.perf_counter()
        profiles = discover_character_profiles(settings)
        persona_by_id = {profile["id"]: load_persona(profile["persona_path"]) for profile in profiles}
        global_stickers = load_global_sticker_catalog(
            resolve_sticker_dir(settings),
            persona_paths=[profile["persona_path"] for profile in profiles],
        )
        image_by_id = {profile["id"]: load_image_catalog(profile["persona_path"]) for profile in profiles}
        timings["persona_ms"] = _ms(stage)

        recall = VectorRecall(store, embeddings, limit=settings.recall_limit)
        runtimes = {
            character_id: PersonRuntime(
                store,
                recall,
                embeddings,
                model,
                persona,
                global_stickers,
                image_by_id[character_id],
            )
            for character_id, persona in persona_by_id.items()
        }
        default_character_id = _default_character_id(settings, profiles)
        runtime = runtimes[default_character_id]
        app_clock = clock or RealClock()
        chat = ChatService(store, runtimes, app_clock)

        # Life simulation is frozen in the current phase and continues to use
        # the configured default character until multi-character life is needed.
        default_persona = persona_by_id[default_character_id]
        life = LifeSimulator(store, embeddings, model, default_persona, runtime)
        ticker = TimeTicker(store, runtime)
        days = DayRunner(store, life, ticker)

        timings["total_ms"] = _ms(total)
        logger.info(
            "app.init timings characters=%d %s",
            len(profiles),
            " ".join(f"{key}={value:.1f}ms" for key, value in timings.items()),
        )
        return AppBundle(settings, store, runtime, runtimes, profiles, chat, life, ticker, days, embeddings, model, app_clock, timings)
    except Exception:
        store.close()
        raise


def build_app(config_path: str = "config.yaml", *, clock: Clock | None = None) -> AppBundle:
    return build_app_from_settings(load_settings(config_path), clock=clock)
