"""Proactive Intent dispatch must re-check its authoritative source scope."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.application.proactive_service import ProactiveService
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
REAL = {"schema_version": 1, "reality": "REAL"}
SIM = {"schema_version": 1, "reality": "SIMULATED", "activity_id": "practice", "branch_id": "other"}


class ReplyModel(PersonModel):
    def __init__(self):
        self.calls = 0

    def react(self, context):
        self.calls += 1
        return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="在吗？")])


class RelabellingModel(PersonModel):
    """Relabels the source after model entry but before the derived commit."""

    def __init__(self, store, source_event_id):
        self.store = store
        self.source_event_id = source_event_id
        self.calls = 0

    def react(self, context):
        self.calls += 1
        self.store.conn.execute(
            "UPDATE conversation_events SET metadata_json=? WHERE id=?",
            (json.dumps({"experience_scope": SIM}), self.source_event_id),
        )
        self.store.conn.commit()
        return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="迟到消息")])


def _runtime(store, model):
    embeddings = DeterministicEmbedding()
    return PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "fixture persona")


def _source(store, *, scope=REAL):
    metadata = {"conversation_id": "direct"} if scope is None else {"conversation_id": "direct", "experience_scope": scope}
    return store.append_message(
        Event(
            character_id="momo",
            event_type=EventType.USER_MESSAGE,
            event_time=NOW - timedelta(hours=2),
            content="下午要汇报",
            metadata=metadata,
        )
    )


def _intent(store, *, source_event_id):
    return store.add_intent(
        "momo",
        "问问汇报结果",
        ActionType.PROACTIVE_MESSAGE.value,
        NOW - timedelta(hours=2),
        NOW - timedelta(hours=1),
        NOW + timedelta(hours=4),
        source_event_id=source_event_id,
    )


def _intent_row(store, intent_id):
    return next(row for row in store.list_intents("momo", limit=30) if row["id"] == intent_id)


def _audit_decisions(store, intent_id):
    return [row["decision"] for row in store.conn.execute(
        "SELECT decision FROM intent_deferral_audit WHERE intent_id=? ORDER BY id", (intent_id,)
    ).fetchall()]


def _proactive_events(store):
    return store.list_events("momo", limit=50, event_type=EventType.PROACTIVE_INTENT.value)


def _character_messages(store):
    return store.list_events("momo", limit=50, event_type=EventType.CHARACTER_MESSAGE.value)


def test_real_source_dispatches_and_derived_event_carries_explicit_real_scope(tmp_path):
    store = SQLiteStore(tmp_path / "real.db")
    source = _source(store)
    intent_id = _intent(store, source_event_id=source.id)
    model = ReplyModel()
    service = ProactiveService(store, ChatService(store, {"momo": _runtime(store, model)}, FixedClock(NOW)))

    outcomes = service.dispatch_due(["momo"], NOW)

    assert outcomes[0]["intent_id"] == intent_id
    assert outcomes[0]["status"] == "EXECUTED"
    assert model.calls == 1
    events = _proactive_events(store)
    assert len(events) == 1
    assert events[0].metadata["experience_scope"] == REAL
    assert events[0].metadata["intent_id"] == intent_id
    assert [message.content for message in _character_messages(store)] == ["在吗？"]
    assert _intent_row(store, intent_id)["status"] == "EXECUTED"
    store.close()


def test_legacy_source_without_scope_is_real_and_new_event_is_explicit(tmp_path):
    store = SQLiteStore(tmp_path / "legacy.db")
    source = _source(store, scope=None)
    intent_id = _intent(store, source_event_id=source.id)
    model = ReplyModel()
    service = ProactiveService(store, ChatService(store, {"momo": _runtime(store, model)}, FixedClock(NOW)))

    outcomes = service.dispatch_due(["momo"], NOW)

    assert outcomes[0]["status"] == "EXECUTED"
    assert _proactive_events(store)[0].metadata["experience_scope"] == REAL
    assert _intent_row(store, intent_id)["status"] == "EXECUTED"
    store.close()


def test_source_relabelled_simulated_is_suppressed_without_model_or_derived_write(tmp_path):
    store = SQLiteStore(tmp_path / "simulated.db")
    source = _source(store)
    intent_id = _intent(store, source_event_id=source.id)
    store.update_event_metadata(source.id, {"experience_scope": SIM})
    model = ReplyModel()
    service = ProactiveService(store, ChatService(store, {"momo": _runtime(store, model)}, FixedClock(NOW)))

    outcomes = service.dispatch_due(["momo"], NOW)

    assert outcomes[0]["status"] == "SUPPRESSED"
    assert outcomes[0]["resolution_reason"] == "SOURCE_NOT_REAL"
    assert outcomes[0]["source_event_id"] == source.id
    assert model.calls == 0
    assert _proactive_events(store) == []
    assert _character_messages(store) == []
    assert store.list_memories("momo") == []
    assert _intent_row(store, intent_id)["status"] == "SUPPRESSED"
    assert _audit_decisions(store, intent_id) == ["SOURCE_NOT_REAL"]
    assert store.proactive_dispatch_state("momo")["last_status"] == "SUPPRESSED"
    store.close()


@pytest.mark.parametrize("source_event_id", [None, 2147483647])
def test_missing_source_is_fail_closed(tmp_path, source_event_id):
    store = SQLiteStore(tmp_path / "missing.db")
    intent_id = _intent(store, source_event_id=source_event_id)
    model = ReplyModel()
    service = ProactiveService(store, ChatService(store, {"momo": _runtime(store, model)}, FixedClock(NOW)))

    outcomes = service.dispatch_due(["momo"], NOW)

    assert outcomes[0]["status"] == "SUPPRESSED"
    assert outcomes[0]["resolution_reason"] == "SOURCE_MISSING"
    assert model.calls == 0
    assert _proactive_events(store) == []
    assert _character_messages(store) == []
    assert store.list_memories("momo") == []
    assert _intent_row(store, intent_id)["status"] == "SUPPRESSED"
    assert _audit_decisions(store, intent_id) == ["SOURCE_MISSING"]
    store.close()


def test_late_relabel_during_model_cannot_commit_real_derived_output(tmp_path):
    store = SQLiteStore(tmp_path / "late.db")
    source = _source(store)
    intent_id = _intent(store, source_event_id=source.id)
    model = RelabellingModel(store, source.id)
    service = ProactiveService(store, ChatService(store, {"momo": _runtime(store, model)}, FixedClock(NOW)))

    outcomes = service.dispatch_due(["momo"], NOW)

    assert outcomes[0]["status"] == "SUPPRESSED"
    assert outcomes[0]["resolution_reason"] == "SOURCE_CHANGED"
    assert model.calls == 1
    assert _character_messages(store) == []
    assert store.list_memories("momo") == []
    assert _intent_row(store, intent_id)["status"] == "SUPPRESSED"
    assert _audit_decisions(store, intent_id) == ["SOURCE_CHANGED"]
    # The tick event itself is a real momentary fact, but no derived trace or
    # output may be committed after the source changed.
    ticks = _proactive_events(store)
    assert len(ticks) == 1
    assert store.get_runtime_trace(ticks[0].id) is None
    store.close()
