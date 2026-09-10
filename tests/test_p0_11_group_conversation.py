from datetime import datetime, timezone
from pathlib import Path

from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    IntentCandidate,
    MemoryCandidate,
    PersonReaction,
)
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class SequenceModel(PersonModel):
    def __init__(self, reactions):
        self.reactions = list(reactions)
        self.contexts = []
        self.sessions = []
        self.last_model = "fake-group-model"

    def react(self, context: str) -> PersonReaction:
        return self.react_for_session(context, "default")

    def react_for_session(self, context: str, session_id: str) -> PersonReaction:
        self.contexts.append(context)
        self.sessions.append(session_id)
        return self.reactions.pop(0)

    def plan_day(self, context: str) -> DailyLifePlan:
        return DailyLifePlan()

    def write_diary(self, context: str) -> DiaryResult:
        return DiaryResult(diary="", mental_state_update="")


def runtime_for(store, embeddings, model, persona):
    return PersonRuntime(
        store,
        VectorRecall(store, embeddings, limit=8),
        embeddings,
        model,
        persona,
    )


def test_group_turn_has_one_shared_fact_and_independent_character_reactions(tmp_path: Path):
    store = SQLiteStore(tmp_path / "group.db")
    now = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
    clock = FixedClock(now)
    embeddings = DeterministicEmbedding()
    model = SequenceModel(
        [
            PersonReaction(
                perception="用户在群里说自己修好了问题。",
                reaction="想简短确认一下。",
                actions=[ActionDecision(type=ActionType.MESSAGE, message="总算修好了。")],
                memory_candidates=[MemoryCandidate(content="用户在群里说自己终于修好了那个问题。", memory_type="SHARED", importance=0.7)],
                intent_candidates=[IntentCandidate(content="晚点再问一次")],
            ),
            PersonReaction(
                perception="Rin 已经回应了用户。",
                reaction="没有必要重复。",
                actions=[],
            ),
        ]
    )
    runtimes = {
        "rin": runtime_for(store, embeddings, model, "你是 Rin。"),
        "momo": runtime_for(store, embeddings, model, "你是 Momo。"),
    }
    service = GroupConversationService(
        store,
        runtimes,
        clock,
        profiles=[{"id": "rin", "name": "Rin"}, {"id": "momo", "name": "Momo"}],
    )
    group = service.create_group("测试群", ["rin", "momo"])
    result = service.send(group.id, "终于修好了")

    events = service.repo.list_events(group.id)
    assert [(item.actor_type, item.actor_id) for item in events] == [
        ("USER", "user"),
        ("CHARACTER", "rin"),
    ]
    assert events[0].content == "终于修好了"
    assert events[1].content == "总算修好了。"
    assert result["speaker_order"] == ["rin", "momo"]

    # The shared user fact is NOT copied into either character-local chat log.
    assert store.list_chat_events("rin") == []
    assert store.list_chat_events("momo") == []

    # Later speakers see earlier character actions from the same group turn.
    assert "Rin: 总算修好了。" in model.contexts[1]
    assert model.sessions == [f"group:{group.id}:rin", f"group:{group.id}:momo"]

    # Group memory remains private to the character and points back through metadata.
    rin_memories = store.list_memories("rin", include_embedding=False)
    assert len(rin_memories) == 1
    assert rin_memories[0].metadata["origin"] == "GROUP"
    assert rin_memories[0].metadata["conversation_id"] == group.id
    assert rin_memories[0].metadata["source_conversation_event_id"] == events[0].id

    # P0.11 V0 explicitly discards group proactive intents.
    assert store.list_intents("rin") == []

    traces = service.repo.list_turn_traces(group.id, result["turn_id"])
    assert [item["character_id"] for item in traces] == ["rin", "momo"]
    assert traces[1]["actions"] == []
    store.close()


def test_group_first_speaker_rotates_between_user_turns(tmp_path: Path):
    store = SQLiteStore(tmp_path / "rotate.db")
    now = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
    clock = FixedClock(now)
    embeddings = DeterministicEmbedding()
    model = SequenceModel([PersonReaction(actions=[]), PersonReaction(actions=[]), PersonReaction(actions=[]), PersonReaction(actions=[])])
    runtimes = {
        "rin": runtime_for(store, embeddings, model, "Rin"),
        "momo": runtime_for(store, embeddings, model, "Momo"),
    }
    service = GroupConversationService(store, runtimes, clock)
    group = service.create_group("轮转群", ["rin", "momo"])

    first = service.send(group.id, "第一轮")
    second = service.send(group.id, "第二轮")
    assert first["speaker_order"] == ["rin", "momo"]
    assert second["speaker_order"] == ["momo", "rin"]
    store.close()


def test_p0_11_web_assets_are_loaded_and_group_submit_is_captured():
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    index = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "p0_11.js").read_text(encoding="utf-8")
    css = (web / "p0_11.css").read_text(encoding="utf-8")

    assert "/static/p0_11.css" in index
    assert "/static/p0_11.js" in index
    assert 'composer.addEventListener("submit"' in js
    assert "event.stopImmediatePropagation();" in js
    assert "/v1/groups/" in js
    assert "group-message-sticker" in css
