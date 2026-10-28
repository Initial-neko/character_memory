from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from character_memory.config import discover_character_profiles, load_persona
from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
from character_memory.llm.client import ModelCallResult, PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class OfflineEvalModel(PersonModel):
    """Deterministic local model used only by the eval harness.

    It proves that the runner, context composition, memory seeds and reporting
    work end to end. It is not a stand-in for model quality and its output is
    explicitly marked ``offline-deterministic`` in every report.
    """

    model = "offline-deterministic-v1"

    def react(self, context: str) -> PersonReaction:
        return PersonReaction(
            perception="离线评测模型读取了当前事件。",
            reaction="离线评测模型生成固定安全回复。",
            actions=[ActionDecision(type=ActionType.MESSAGE, message="我在。")],
        )


class PaidCallBudgetExceeded(RuntimeError):
    """Raised before a live provider call would exceed the eval budget."""


class BudgetedModel:
    """Pre-call budget gate for live eval runs.

    The check happens before delegating, so a 10-call budget can never spend an
    eleventh real call, even when a provider retry is requested inside.
    """

    def __init__(self, model, max_calls: int = 10):
        self._model = model
        self.max_calls = max(0, int(max_calls))
        self.used = 0

    def __getattr__(self, name):
        return getattr(self._model, name)

    def _guard(self) -> None:
        if self.used >= self.max_calls:
            raise PaidCallBudgetExceeded(
                f"paid eval call budget exhausted (max={self.max_calls}); "
                "no further provider call was made"
            )
        self.used += 1

    def react(self, context: str) -> PersonReaction:
        self._guard()
        return self._model.react(context)

    def react_for_session(self, context: str, session_id: str) -> PersonReaction:
        self._guard()
        return self._model.react_for_session(context, session_id)

    def react_call_for_session(self, context: str, session_id: str) -> ModelCallResult:
        self._guard()
        return self._model.react_call_for_session(context, session_id)

    def react_with_images_for_session(self, context: str, image_data_urls, session_id: str) -> PersonReaction:
        self._guard()
        return self._model.react_with_images_for_session(context, image_data_urls, session_id)

    def react_call_with_images_for_session(self, context: str, image_data_urls, session_id: str) -> ModelCallResult:
        self._guard()
        return self._model.react_call_with_images_for_session(context, image_data_urls, session_id)

    def structured_call_for_session(self, prompt: str, schema, session_id: str) -> ModelCallResult:
        self._guard()
        return self._model.structured_call_for_session(prompt, schema, session_id)

    def structured_for_session(self, prompt: str, schema, session_id: str):
        self._guard()
        return self._model.structured_for_session(prompt, schema, session_id)

    def complete_text_for_session(self, messages: list[dict], session_id: str) -> str:
        self._guard()
        return self._model.complete_text_for_session(messages, session_id)

    def close(self) -> None:
        self._model.close()


class TeeUsageRecorder:
    """Record the eval meter while preserving the normal usage database row."""

    def __init__(self, meter, inner=None):
        self.meter = meter
        self.inner = inner

    def record(self, item: dict) -> int | None:
        self.meter.record(item)
        if self.inner is not None:
            return self.inner.record(item)
        return None

    def close(self) -> None:
        if self.inner is not None:
            self.inner.close()


@dataclass
class OfflineEvalBundle:
    settings: object
    store: SQLiteStore
    embeddings: DeterministicEmbedding
    model: OfflineEvalModel
    runtimes: dict[str, PersonRuntime]

    def persona_versions(self) -> dict[str, str]:
        return {
            character_id: hashlib.sha256(runtime.persona.encode("utf-8")).hexdigest()[:16]
            for character_id, runtime in sorted(self.runtimes.items())
        }

    def close(self) -> None:
        try:
            close_embeddings = getattr(self.embeddings, "close", None)
            if callable(close_embeddings):
                close_embeddings()
        finally:
            self.store.close()


def build_offline_eval_bundle(settings, *, character_ids=None) -> OfflineEvalBundle:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(settings.db_path)
    embeddings = DeterministicEmbedding()
    model = OfflineEvalModel()
    recall = VectorRecall(store, embeddings)
    profiles = discover_character_profiles(settings)
    persona_by_id = {
        profile["id"]: load_persona(profile["persona_path"]) for profile in profiles
    }
    requested = set(character_ids or persona_by_id)
    runtimes = {
        character_id: PersonRuntime(store, recall, embeddings, model, persona)
        for character_id, persona in persona_by_id.items()
        if character_id in requested
    }
    return OfflineEvalBundle(settings, store, embeddings, model, runtimes)


def persona_versions_from_runtimes(runtimes) -> dict[str, str]:
    versions = {}
    for character_id, runtime in sorted(runtimes.items()):
        persona = getattr(runtime, "persona", None)
        if isinstance(persona, str) and persona:
            versions[character_id] = hashlib.sha256(persona.encode("utf-8")).hexdigest()[:16]
    return versions
