"""The Intent self-loop: a due intent must not be able to schedule its successor.

Before this was bounded, every dispatch produced another intent whose
``earliest_hours`` defaulted to 0, so it was due on the very next 30-second poll.
These tests pin the hard edge for the three event families that must not plan a
future Intent of their own.
"""

from datetime import datetime, timedelta, timezone

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    Event,
    EventType,
    IntentCandidate,
    PersonReaction,
)
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import IntentPolicy, PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class AlwaysIntentModel(PersonModel):
    """Every round tries to leave another future Intent -- the production shape."""

    def react(self, context):
        return PersonReaction(
            actions=[ActionDecision(type=ActionType.MESSAGE, message="明早给你看叶子")],
            intent_candidates=[IntentCandidate(content="明早给茉莉叶子拍照发给铃")],
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def _runtime(store, model, **policy):
    embeddings = DeterministicEmbedding()
    return PersonRuntime(
        store,
        VectorRecall(store, embeddings),
        embeddings,
        model,
        "persona",
        intent_policy=IntentPolicy(**policy),
    )


def test_proactive_intent_turn_cannot_leave_another_intent(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "loop.db")
    runtime = _runtime(store, AlwaysIntentModel())
    service = ChatService(store, {"momo": runtime}, FixedClock(t0))
    original = store.add_intent(
        "momo",
        "明早看叶子",
        ActionType.PROACTIVE_MESSAGE.value,
        t0 - timedelta(hours=2),
        t0 - timedelta(hours=1),
        t0 + timedelta(hours=4),
    )

    result = service.dispatch_proactive_intent(
        character_id="momo",
        intent_id=original,
        content="明早看叶子",
        at=t0,
    )

    # The round itself still happened: the character said its line.
    messages = store.list_events("momo", limit=10, event_type=EventType.CHARACTER_MESSAGE.value)
    assert [message.content for message in messages] == ["明早给你看叶子"]

    # And it left nothing behind for the next poll to pick up.
    assert result.created_intent_ids == []
    assert store.pending_intent_count("momo") == 1
    trace = store.get_runtime_trace(result.event.id)
    assert [item["decision"] for item in trace["channel_decisions"]] == ["DROP_PROACTIVE_SELF_LOOP"]
    assert trace["intent_candidates"] == []
    store.close()


def test_space_and_world_events_cannot_plan_a_private_intent(tmp_path):
    """Their intents would have come due as *private* proactive messages."""
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "channel.db")
    runtime = _runtime(store, AlwaysIntentModel())

    for event_type, content in (
        (EventType.SPACE_COMMENT_RECEIVED, "有人评论了你的动态"),
        (EventType.SPACE_POST_SEEN, "另一位角色发布了动态"),
        (EventType.WORLD_OBSERVATION, "你在网上看到一件事"),
    ):
        result = runtime.handle(
            Event(
                character_id="momo",
                event_type=event_type,
                event_time=t0,
                content=content,
                metadata={"conversation_id": f"space:{event_type.value}"},
            )
        )
        assert result.created_intent_ids == [], event_type
        trace = store.get_runtime_trace(result.event.id)
        assert [item["decision"] for item in trace["channel_decisions"]][0] == "DROP_CHANNEL_CANNOT_PLAN_INTENT"

    assert store.pending_intent_count("momo") == 0
    store.close()


def test_user_message_can_still_plan_an_intent_but_not_before_the_floor(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "floor.db")
    runtime = _runtime(store, AlwaysIntentModel(), min_delay_minutes=10)
    service = ChatService(store, {"momo": runtime}, FixedClock(t0))

    result = service.send("帮我记着明早看叶子", character_id="momo", conversation_id="cli", at=t0)

    assert len(result.created_intent_ids) == 1
    row = next(item for item in store.list_intents("momo") if item["id"] == result.created_intent_ids[0])
    assert datetime.fromisoformat(row["earliest_at"]) == t0 + timedelta(minutes=10)
    # The clamp moves the time, never the meaning.
    assert row["content"] == "明早给茉莉叶子拍照发给铃"
    store.close()
