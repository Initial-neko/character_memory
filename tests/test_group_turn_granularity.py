from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import threading
import time

from character_memory.application.async_conversation import (
    ConversationEventHub,
    ReactionScheduler,
    group_channel,
)
from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    PersonReaction,
)
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


MEMBERS = ["a", "b", "c", "d", "e", "f"]


class SilentSequenceModel(PersonModel):
    """One reaction per call, in order; every visible action is optional."""

    def __init__(self, count):
        self.remaining = count
        self.contexts = []

    def react(self, context: str) -> PersonReaction:
        self.contexts.append(context)
        if self.remaining <= 0:
            raise AssertionError("model consumed more reactions than the test provided")
        self.remaining -= 1
        return PersonReaction(actions=[])

    def plan_day(self, context: str) -> DailyLifePlan:
        return DailyLifePlan()

    def write_diary(self, context: str) -> DiaryResult:
        return DiaryResult(diary="", mental_state_update="")


def build_service(tmp_path: Path, members: list[str], reactions: int):
    store = SQLiteStore(tmp_path / "group-turn.db")
    embeddings = DeterministicEmbedding()
    model = SilentSequenceModel(reactions)
    runtimes = {
        member: PersonRuntime(store, VectorRecall(store, embeddings, limit=8), embeddings, model, member)
        for member in members
    }
    service = GroupConversationService(
        store,
        runtimes,
        FixedClock(datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)),
        profiles=[{"id": member, "name": member.upper()} for member in members],
    )
    return service, model


def test_max_speakers_caps_unmentioned_members_and_keeps_rotation(tmp_path: Path):
    """A capped turn asks 5 of 6 members, and the deferred seat rotates.

    The cap truncates unmentioned members from the tail of the rotating order,
    so the member who sat out on turn one is inside the cap on turn two while a
    different member sits out. Silence stays an individual decision; the cap
    only decides who is asked.
    """
    service, _model = build_service(tmp_path, MEMBERS, reactions=10)
    group = service.create_group("六人群", MEMBERS)

    first = service.react_from_event(
        service.persist_user_event(group.id, "第一轮"),
        max_speakers=5,
    )
    assert first["speaker_order"] == ["a", "b", "c", "d", "e"]
    assert first["deferred_speaker_ids"] == ["f"]
    assert first["max_speakers"] == 5

    second = service.react_from_event(
        service.persist_user_event(group.id, "第二轮"),
        max_speakers=5,
    )
    assert second["speaker_order"] == ["b", "c", "d", "e", "f"]
    assert second["deferred_speaker_ids"] == ["a"]
    store = service.store
    store.close()


def test_mentioned_members_are_never_deferred_by_the_cap(tmp_path: Path):
    """Being @'d and then silenced is a different bug from a crowded room.

    The named members consume the cap first; only the unmentioned tail is cut.
    """
    service, _model = build_service(tmp_path, MEMBERS[:4], reactions=4)
    group = service.create_group("四人群", MEMBERS[:4])

    result = service.react_from_event(
        service.persist_user_event(group.id, "看看谁在"),
        mention_order=["c", "d"],
        max_speakers=3,
    )
    assert result["speaker_order"] == ["c", "d", "a"]
    assert result["deferred_speaker_ids"] == ["b"]

    everyone = service.react_from_event(
        service.persist_user_event(group.id, "@所有人"),
        mention_order=["*"],
        max_speakers=2,
    )
    assert everyone["speaker_order"] == ["a", "b", "c", "d"]
    assert everyone["deferred_speaker_ids"] == []
    store = service.store
    store.close()


def test_missing_cap_keeps_the_every_member_is_asked_behavior(tmp_path: Path):
    """`max_speakers=None` is the direct-caller escape hatch, not a default of 1.

    Callers that do not know about the knob (and every pre-existing test) must
    keep seeing the legacy full-room order.
    """
    service, _model = build_service(tmp_path, MEMBERS[:3], reactions=3)
    group = service.create_group("三人群", MEMBERS[:3])

    result = service.react_from_event(service.persist_user_event(group.id, "不加上限"))
    assert result["speaker_order"] == ["a", "b", "c"]
    assert result["deferred_speaker_ids"] == []
    assert result["max_speakers"] is None
    store = service.store
    store.close()


def test_deferred_members_leave_no_decision_and_no_trace(tmp_path: Path):
    """Deferred is an scheduling outcome, not a silent reaction.

    A deferred member must not gain a trace (which would read as "decided to
    stay silent") nor consume a model call.
    """
    service, model = build_service(tmp_path, MEMBERS, reactions=5)
    group = service.create_group("延迟群", MEMBERS)
    source = service.persist_user_event(group.id, "第一轮")
    result = service.react_from_event(source, max_speakers=5)

    traces = service.repo.list_turn_traces(group.id, source.turn_id)
    assert sorted(item["character_id"] for item in traces) == ["a", "b", "c", "d", "e"]
    assert "f" not in [item["character_id"] for item in traces]
    assert len(model.contexts) == 5
    assert result["deferred_speaker_ids"] == ["f"]
    store = service.store
    store.close()


class GroupSequenceModel(PersonModel):
    def __init__(self, reactions):
        self.reactions = list(reactions)
        self.contexts = []

    def react(self, context: str) -> PersonReaction:
        self.contexts.append(context)
        return self.reactions.pop(0)

    def plan_day(self, context: str) -> DailyLifePlan:
        return DailyLifePlan()

    def write_diary(self, context: str) -> DiaryResult:
        return DiaryResult(diary="", mental_state_update="")


def test_group_reaction_complete_reports_cap_and_deferred_speakers(tmp_path: Path):
    """The scheduler reports `deferred_speaker_ids` / `max_speakers` on SSE.

    The UI does not need them to render (folding is driven by turn_id), but the
    payload is the operator-visible contract that a cap actually applied.
    """
    store = SQLiteStore(tmp_path / "group-hub.db")
    embeddings = DeterministicEmbedding()
    members = MEMBERS[:4]
    model = GroupSequenceModel([PersonReaction(actions=[]) for _ in members])
    runtimes = {
        member: PersonRuntime(store, VectorRecall(store, embeddings, limit=8), embeddings, model, member)
        for member in members
    }

    class _LockService:
        def __init__(self):
            self.lock = threading.RLock()

        def _lock_for(self, _character_id):
            return self.lock

    service = GroupConversationService(
        store,
        runtimes,
        FixedClock(datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)),
        chat_service=_LockService(),
        profiles=[{"id": member, "name": member.upper()} for member in members],
    )
    group = service.create_group("回报群", members)
    source = service.persist_user_event(group.id, "谁来接话")

    hub = ConversationEventHub()
    channel = group_channel(group.id)
    bundle = SimpleNamespace(
        store=store,
        runtimes=runtimes,
        clock=service.clock,
        chat=_LockService(),
        settings=SimpleNamespace(group_max_speakers_per_turn=3),
    )
    scheduler = ReactionScheduler(
        lambda: bundle,
        lambda: [{"id": member, "name": member.upper()} for member in members],
        hub,
        quiet_seconds=0.01,
        max_burst_seconds=0.05,
    )
    scheduler.enqueue_group(group.id, source)
    deadline = time.monotonic() + 10.0
    completed = None
    while time.monotonic() < deadline and completed is None:
        for _seq, event_type, data in list(hub._channel(channel).events):
            if event_type == "reaction_complete":
                completed = data
        if completed is None:
            time.sleep(0.05)
    scheduler.close()
    hub.close()

    assert completed is not None, "scheduler never published reaction_complete"
    assert completed["max_speakers"] == 3
    assert completed["deferred_speaker_ids"] == [members[3]]
    assert completed["speaker_order"] == members[:3]
    store.close()
