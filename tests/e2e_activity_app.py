"""Isolated real HTTP application for Activity lifecycle acceptance.

The model is deterministic and records every reaction call to a log file, so the
acceptance test can prove that reading or resuming an activity never touches it.
No provider, network or embedding download is involved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from character_memory.activity_web import attach_activity_routes
from character_memory.api import create_api
from character_memory.application.chat_service import ChatService
from character_memory.application.clock import RealClock
from character_memory.config import Settings, discover_character_profiles, load_persona
from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ["CHARACTER_MEMORY_ACTIVITY_DB"])
MEDIA_DIR = Path(os.environ["CHARACTER_MEMORY_ACTIVITY_MEDIA"])
PERSONA_ROOT = Path(os.environ["CHARACTER_MEMORY_ACTIVITY_PERSONAS"])
CALL_LOG = Path(os.environ["ACTIVITY_MODEL_CALL_LOG"])


class RecordingModel(PersonModel):
    model = "e2e-activity-fake"
    vision_model = "e2e-activity-fake"

    def react(self, context: str) -> PersonReaction:
        with CALL_LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "kind": "react"}) + "\n")
        return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="activity reply")])


settings = Settings(
    api_key="e2e-activity-key",
    embedding_provider="deterministic",
    db_path=str(DB_PATH),
    media_dir=str(MEDIA_DIR),
    sticker_dir=str(DB_PATH.parent / "activity-stickers"),
    avatar_dir=str(DB_PATH.parent / "activity-avatars"),
    persona_path=str(PERSONA_ROOT / "rin" / "persona.yaml"),
    proactive_wake_enabled=False,
)
store = SQLiteStore(settings.db_path)
embeddings = DeterministicEmbedding()
model = RecordingModel()
profiles = discover_character_profiles(settings)
personas = {profile["id"]: load_persona(profile["persona_path"]) for profile in profiles}
recall = VectorRecall(store, embeddings, limit=settings.recall_limit)
runtimes = {
    character_id: PersonRuntime(store, recall, embeddings, model, persona)
    for character_id, persona in personas.items()
}
clock = RealClock()
bundle = SimpleNamespace(
    settings=settings,
    store=store,
    runtime=next(iter(runtimes.values())),
    runtimes=runtimes,
    characters=profiles,
    chat=ChatService(store, runtimes, clock),
    embeddings=embeddings,
    model=model,
    clock=clock,
    init_timings={"total_ms": 0.0},
)

app = create_api(bundle=bundle)
attach_activity_routes(app)


@app.get("/__e2e/activity-runtime")
def runtime():
    calls = 0
    if CALL_LOG.exists():
        calls = len(CALL_LOG.read_text(encoding="utf-8").splitlines())
    return {"pid": os.getpid(), "model_calls": calls}


def _close_test_store() -> None:
    store.close()


app.router.on_shutdown.append(_close_test_store)
