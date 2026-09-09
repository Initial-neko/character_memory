from datetime import datetime, timezone

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.config import Settings, discover_character_profiles, resolve_persona_path
from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class PersonaAwareFake(PersonModel):
    def react(self, context):
        if "MOMO_MARK" in context:
            message = "momo"
        elif "HARU_MARK" in context:
            message = "haru"
        else:
            message = "unknown"
        return PersonReaction(
            perception="收到",
            reaction="回应",
            mental_state_update="平静",
            action=ActionDecision(type=ActionType.REPLY, reason="测试", message=message),
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="平静。", mental_state_update="平静")


def test_persona_files_are_discovered_from_personas_directory():
    settings = Settings(persona_path="personas/rin/persona.yaml")
    profiles = discover_character_profiles(settings)
    ids = {profile["id"] for profile in profiles}
    assert {"rin", "momo", "haru", "rei"}.issubset(ids)
    assert resolve_persona_path(settings, "momo").endswith("personas/momo/persona.yaml")


def test_chat_service_routes_character_to_its_persona_runtime(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    model = PersonaAwareFake()
    recall = VectorRecall(store, emb)
    runtimes = {
        "momo": PersonRuntime(store, recall, emb, model, "MOMO_MARK"),
        "haru": PersonRuntime(store, recall, emb, model, "HARU_MARK"),
    }
    service = ChatService(store, runtimes, FixedClock(datetime(2026, 9, 9, 10, tzinfo=timezone.utc)))

    momo = service.send("你好", character_id="momo", conversation_id="momo-session")
    haru = service.send("你好", character_id="haru", conversation_id="haru-session")

    assert momo.reaction.action.message == "momo"
    assert haru.reaction.action.message == "haru"
    assert [item["content"] for item in service.history("momo")["messages"]] == ["你好", "momo"]
    assert [item["content"] for item in service.history("haru")["messages"]] == ["你好", "haru"]
    store.close()
