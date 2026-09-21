from __future__ import annotations

from pathlib import Path
import os
import re
from types import SimpleNamespace

from character_memory.api import create_api
from character_memory.application.chat_service import ChatService
from character_memory.application.clock import RealClock
from character_memory.async_web import attach_async_routes
from character_memory.avatar_web import attach_avatar_routes
from character_memory.config import Settings, discover_character_profiles, load_persona, resolve_sticker_dir
from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, PersonReaction
from character_memory.group_web import attach_group_routes
from character_memory.history_web import attach_history_routes
from character_memory.images import load_image_catalog
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.search_web import attach_search_routes
from character_memory.stickers import load_global_sticker_catalog
from character_memory.storage.sqlite import SQLiteStore
from character_memory.wake_web import attach_wake_routes


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("CHARACTER_MEMORY_E2E_DB", ROOT / ".e2e-realtime.db"))
MEDIA_DIR = Path(os.environ.get("CHARACTER_MEMORY_E2E_MEDIA", DB_PATH.parent / "e2e-media"))
# Overridable because archiving a character writes a marker into its directory:
# a browser test that exercises archiving must not touch the repo's personas.
PERSONA_ROOT = Path(os.environ.get("CHARACTER_MEMORY_E2E_PERSONA_ROOT", ROOT / "personas"))


class DeterministicPersonModel(PersonModel):
    model = "e2e-fake-text"
    vision_model = "e2e-fake-vision"

    @staticmethod
    def _current_text(context: str) -> str:
        block = context.rsplit("# Current Event", 1)[-1]
        match = re.search(r"(?:USER_MESSAGE|TIME_TICK):\s*([^\n]+)", block)
        if not match:
            return "event"
        text = " ".join(match.group(1).split()).strip()
        if text.startswith("群聊当前最新用户事实："):
            text = text.split("：", 1)[1]
        return text[:100]

    def _reaction(self, context: str) -> PersonReaction:
        current = self._current_text(context)
        if "# Group Conversation Contract" in context:
            message = f"E2E group reply: {current}"
        elif "TIME_TICK" in context or "手动唤醒后的主动判断" in context or "时间自然过去了一段" in context:
            message = "E2E wake reply"
        else:
            message = f"E2E reply: {current}"
        return PersonReaction(
            perception="E2E deterministic perception",
            reaction="E2E deterministic reaction",
            actions=[ActionDecision(type=ActionType.MESSAGE, message=message)],
            memory_candidates=[],
            intent_candidates=[],
        )

    def react(self, context: str) -> PersonReaction:
        return self._reaction(context)

    def react_for_session(self, context: str, session_id: str) -> PersonReaction:
        return self._reaction(context)

    def react_with_images_for_session(self, context: str, image_data_urls: list[str], session_id: str) -> PersonReaction:
        return self._reaction(context)

    def plan_day(self, context: str) -> DailyLifePlan:
        return DailyLifePlan()

    def write_diary(self, context: str) -> DiaryResult:
        return DiaryResult(diary="E2E", mental_state_update="", memory_candidates=[])


settings = Settings(
    api_key="e2e-test-key",
    embedding_provider="deterministic",
    db_path=str(DB_PATH),
    media_dir=str(MEDIA_DIR),
    sticker_dir=str(DB_PATH.parent / "e2e-stickers"),
    avatar_dir=str(DB_PATH.parent / "e2e-avatars"),
    persona_path=str(PERSONA_ROOT / "rin" / "persona.yaml"),
    proactive_wake_enabled=False,
)
store = SQLiteStore(settings.db_path)
embeddings = DeterministicEmbedding()
model = DeterministicPersonModel()
profiles = discover_character_profiles(settings)
personas = {profile["id"]: load_persona(profile["persona_path"]) for profile in profiles}
stickers = load_global_sticker_catalog(
    resolve_sticker_dir(settings),
    persona_paths=[profile["persona_path"] for profile in profiles],
)
recall = VectorRecall(store, embeddings, limit=settings.recall_limit)
runtimes = {
    character_id: PersonRuntime(
        store,
        recall,
        embeddings,
        model,
        persona,
        stickers,
        load_image_catalog(next(profile["persona_path"] for profile in profiles if profile["id"] == character_id)),
    )
    for character_id, persona in personas.items()
}
clock = RealClock()
chat = ChatService(store, runtimes, clock)
bundle = SimpleNamespace(
    settings=settings,
    store=store,
    runtime=next(iter(runtimes.values())),
    runtimes=runtimes,
    characters=profiles,
    chat=chat,
    embeddings=embeddings,
    model=model,
    clock=clock,
    init_timings={"total_ms": 0.0},
)

app = create_api(bundle=bundle)
# The sidebar's avatar module reads /v1/character-profiles on load. Without this
# the route 404s in every browser run and the console-error check these tests
# rely on can never be clean, which is the one thing that makes it useful.
attach_avatar_routes(app)
attach_history_routes(app)
attach_group_routes(app, str(ROOT / "config.example.yaml"))
attach_search_routes(app)
attach_async_routes(app)
attach_wake_routes(app)


def _close_test_store():
    store.close()


app.router.on_shutdown.append(_close_test_store)
