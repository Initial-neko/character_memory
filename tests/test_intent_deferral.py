from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from character_memory.application.proactive_service import ProactiveService
from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
from character_memory.storage.sqlite import SQLiteStore
from test_proactive_service import FakeChat, _intent

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def defer(hours=3):
    return PersonReaction(actions=[], intent_resolution={"kind": "DEFER", "defer_hours": hours})


def test_defer_preserves_id_expiry_and_reopens_after_three_hours_and_restart(tmp_path):
    path = tmp_path / "local.sqlite"
    store = SQLiteStore(path)
    intent_id = _intent(store, "a", NOW, expires_delta=12)
    original = dict(store.list_intents("a")[0])
    chat = FakeChat(defer())
    result = ProactiveService(store, chat).dispatch_due(["a"], NOW)
    assert result[0]["status"] == "PENDING"
    row = store.list_intents("a")[0]
    assert row["id"] == intent_id and row["content"] == original["content"]
    assert row["earliest_at"] == (NOW + timedelta(hours=3)).isoformat()
    assert row["expires_at"] == original["expires_at"] and row["deferral_count"] == 1
    assert len(chat.calls) == 1 and len(store.list_intents("a")) == 1
    store.close()
    store = SQLiteStore(path)
    service = ProactiveService(store, FakeChat())
    assert not service.has_due(["a"], NOW + timedelta(hours=2, minutes=59))
    assert service.has_due(["a"], NOW + timedelta(hours=3))
    assert service.dispatch_due(["a"], NOW + timedelta(hours=3))[0]["status"] == "EXECUTED"
    audit = store.intent_deferral_audit(intent_id)
    assert len(audit) == 1 and audit[0]["decision"] == "DEFERRED"
    assert audit[0]["source_event_id"] == 99
    store.close()


@pytest.mark.parametrize("hours,expires,reason", [(3, 2, "BEYOND_EXPIRY"), (0.01, 12, "OUT_OF_RANGE"),
                                                    (73, 100, "OUT_OF_RANGE")])
def test_invalid_or_expired_deferral_terminates_without_new_intent(tmp_path, hours, expires, reason):
    store = SQLiteStore(tmp_path / "local.sqlite")
    intent_id = _intent(store, "a", NOW, expires_delta=expires)
    result = ProactiveService(store, FakeChat(defer(hours))).dispatch_due(["a"], NOW)
    assert result[0]["status"] == "ABANDONED"
    assert result[0]["resolution_reason"] == reason
    assert not store.due_intents("a", NOW + timedelta(hours=1))
    assert len(store.list_intents("a")) == 1
    assert store.intent_deferral_audit(intent_id)[0]["decision"] == reason
    store.close()


def test_deferral_count_is_bounded_and_legacy_defer_is_not_fabricated(tmp_path):
    store = SQLiteStore(tmp_path / "local.sqlite")
    intent_id = _intent(store, "a", NOW, expires_delta=24)
    service = ProactiveService(store, FakeChat(defer(1)))
    assert service.dispatch_due(["a"], NOW)[0]["status"] == "PENDING"
    assert service.dispatch_due(["a"], NOW + timedelta(hours=1))[0]["status"] == "PENDING"
    last = service.dispatch_due(["a"], NOW + timedelta(hours=2))[0]
    assert last["status"] == "ABANDONED" and last["resolution_reason"] == "MAX_DEFERRALS"
    assert store.list_intents("a")[0]["deferral_count"] == 2
    assert len(store.intent_deferral_audit(intent_id)) == 3
    _intent(store, "b", NOW)
    legacy = PersonReaction(action=ActionDecision(type=ActionType.DEFER))
    result = ProactiveService(store, FakeChat(legacy)).dispatch_due(["b"], NOW)[0]
    assert result["status"] == "ABANDONED" and result["resolution_reason"] == "LEGACY_DEFER_WITHOUT_TIME"
    store.close()


def test_expression_wins_over_conflicting_deferral_and_abandon_is_terminal(tmp_path):
    store = SQLiteStore(tmp_path / "local.sqlite")
    intent_id = _intent(store, "a", NOW)
    reaction = PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="你好")],
                              intent_resolution={"kind": "DEFER", "defer_hours": 3})
    result = ProactiveService(store, FakeChat(reaction)).dispatch_due(["a"], NOW)[0]
    assert result["status"] == "EXECUTED" and result["resolution_reason"] == "EXPRESSION_CONFLICT"
    assert store.intent_deferral_audit(intent_id)[0]["decision"] == "EXPRESSION_CONFLICT"
    _intent(store, "b", NOW)
    result = ProactiveService(store, FakeChat(PersonReaction(actions=[], intent_resolution={"kind": "ABANDON"}))).dispatch_due(["b"], NOW)[0]
    assert result["status"] == "ABANDONED"
    store.close()


def test_cross_connection_claim_consumes_cooldown_and_one_provider_call(tmp_path):
    path = tmp_path / "local.sqlite"
    seed = SQLiteStore(path)
    _intent(seed, "a", NOW)
    seed.close()
    chat = FakeChat(defer())
    def submit(_):
        store = SQLiteStore(path)
        try:
            return ProactiveService(store, chat).dispatch_due(["a"], NOW)
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sum(len(r) for r in results) == 1 and len(chat.calls) == 1
    store = SQLiteStore(path)
    assert store.proactive_dispatch_state("a")["last_status"] == "PENDING"
    store.close()


def test_processing_after_crash_is_not_automatically_replayed(tmp_path):
    path = tmp_path / "local.sqlite"
    store = SQLiteStore(path)
    intent_id = _intent(store, "a", NOW)
    row = store.claim_due_intent("a", NOW, next_allowed_at=NOW + timedelta(hours=1), interval_minutes=60)
    assert row["id"] == intent_id and row["status"] == "PROCESSING"
    store.close()
    store = SQLiteStore(path)
    chat = FakeChat()
    assert ProactiveService(store, chat).dispatch_due(["a"], NOW + timedelta(hours=2)) == []
    assert chat.calls == [] and store.list_intents("a")[0]["status"] == "PROCESSING"
    store.close()


@pytest.mark.parametrize("hours", [None, float("inf"), float("nan"), -1])
def test_missing_nonfinite_negative_deferral_never_reschedules(tmp_path, hours):
    store = SQLiteStore(tmp_path / "local.sqlite")
    _intent(store, "a", NOW)
    result = ProactiveService(store, FakeChat(defer(hours))).dispatch_due(["a"], NOW)[0]
    assert result["status"] == "ABANDONED"
    assert result["resolution_reason"] in {"MISSING_DEFER_TIME", "OUT_OF_RANGE"}
    store.close()


def test_custom_deferral_policy_zero_and_prompt_use_actual_configuration(tmp_path):
    from test_proactive_runtime import ProactiveFakeModel
    from character_memory.application.chat_service import ChatService
    from character_memory.application.clock import FixedClock
    from character_memory.memory.embedding import DeterministicEmbedding
    from character_memory.memory.recall import VectorRecall
    from character_memory.runtime.person_runtime import PersonRuntime
    class Model(ProactiveFakeModel):
        def __init__(self):
            self.calls = []
        def react(self, context):
            self.calls.append(context)
            return defer(3)
    store = SQLiteStore(tmp_path / "local.sqlite")
    _intent(store, "a", NOW, expires_delta=12)
    model = Model()
    embedding = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, embedding), embedding, model, "id: a")
    chat = ChatService(store, {"a": runtime}, FixedClock(NOW))
    result = ProactiveService(store, chat, max_deferrals=0, defer_min_minutes=30, defer_max_hours=6).dispatch_due(["a"], NOW)[0]
    assert result["status"] == "ABANDONED" and result["resolution_reason"] == "MAX_DEFERRALS"
    assert len(model.calls) == 1
    assert "'min_minutes': 30" in model.calls[0] and "'max_hours': 6" in model.calls[0]
    assert "'max_deferrals': 0" in model.calls[0] and "defer_hours" in model.calls[0]
    event = next(e for e in store.list_events("a") if e.event_type.value == "PROACTIVE_INTENT")
    trace = store.get_runtime_trace(event.id)
    assert trace["reaction"] == ""
    assert trace["event"]["metadata"]["intent_deferral_policy"]["max_deferrals"] == 0
    store.close()


def test_optional_malformed_resolution_preserves_legal_message_and_old_json():
    from character_memory.domain.models import IntentResolutionKind
    reaction = PersonReaction.model_validate({"actions": [{"type": "MESSAGE", "message": "你好"}],
                                              "intent_resolution": {"kind": "unknown", "defer_hours": "bad"}})
    assert reaction.actions[0].message == "你好"
    assert reaction.intent_resolution.kind == IntentResolutionKind.ABANDON
    assert PersonReaction.model_validate({"actions": []}).intent_resolution is None


def test_non_proactive_resolution_is_rejected_without_rescheduling(tmp_path):
    from test_proactive_runtime import ProactiveFakeModel
    from character_memory.memory.embedding import DeterministicEmbedding
    from character_memory.memory.recall import VectorRecall
    from character_memory.runtime.person_runtime import PersonRuntime
    from character_memory.domain.models import Event, EventType
    class Model(ProactiveFakeModel):
        def react(self, context):
            return defer(3)
    store = SQLiteStore(tmp_path / "local.sqlite")
    intent_id = _intent(store, "a", NOW)
    before = dict(store.list_intents("a")[0])
    embedding = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, embedding), embedding, Model(), "id: a")
    result = runtime.handle(Event(character_id="a", event_type=EventType.USER_MESSAGE, event_time=NOW, content="你好"))
    assert result.reaction.intent_resolution is None
    assert store.list_intents("a")[0] == before
    assert store.get_runtime_trace(result.event.id)["channel_decisions"] == [
        {"type": "INTENT_RESOLUTION", "decision": "DROP_INTENT_RESOLUTION_WRONG_CHANNEL"}]
    assert store.intent_deferral_audit(intent_id) == []
    store.close()


def test_legacy_deferred_rows_and_times_are_preserved_through_migration(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.sqlite"
    store = SQLiteStore(path)
    intent_id = _intent(store, "a", NOW)
    store.set_intent_status(intent_id, "DEFERRED")
    row = store.list_intents("a")[0]
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM schema_migrations WHERE name='core/010-intent-deferral'")
    connection.execute("DROP TABLE intent_deferral_audit")
    connection.execute("ALTER TABLE intents DROP COLUMN deferral_count")
    connection.commit()
    connection.close()
    reopened = SQLiteStore(path)
    after = reopened.list_intents("a")[0]
    assert after["status"] == "DEFERRED" and after["deferral_count"] == 0
    assert all(after[key] == row[key] for key in ("id", "content", "created_at", "earliest_at", "expires_at"))
    assert not reopened.due_intents("a", NOW + timedelta(hours=1))
    reopened.close()


def test_deferral_and_audit_roll_back_together_and_local_time_crosses_day(tmp_path):
    import sqlite3
    from character_memory.time_utils import epoch_us
    store = SQLiteStore(tmp_path / "local.sqlite")
    local = datetime(2026, 10, 8, 23, 30, tzinfo=timezone(timedelta(hours=8)))
    intent_id = _intent(store, "a", local, expires_delta=12)
    store.claim_due_intent("a", local, next_allowed_at=local + timedelta(hours=1), interval_minutes=60)
    store.conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON intent_deferral_audit BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
    store.conn.commit()
    original = dict(store.list_intents("a")[0])
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        store.defer_intent(intent_id, now=local, defer_hours=3)
    assert store.list_intents("a")[0] == original
    store.conn.execute("DROP TRIGGER reject_audit")
    store.conn.commit()
    result = store.defer_intent(intent_id, now=local, defer_hours=3)
    expected = local + timedelta(hours=3)
    assert result["status"] == "PENDING" and expected.day == 9
    row = store.list_intents("a")[0]
    assert row["earliest_at"] == expected.isoformat() and row["earliest_at_epoch"] == epoch_us(expected)
    store.close()


def test_cooldown_still_blocks_short_deferral_after_it_becomes_due(tmp_path):
    store = SQLiteStore(tmp_path / "local.sqlite")
    _intent(store, "a", NOW)
    chat = FakeChat(defer(0.25))
    service = ProactiveService(store, chat)
    assert service.dispatch_due(["a"], NOW)[0]["status"] == "PENDING"
    assert store.due_intents("a", NOW + timedelta(minutes=15))
    assert not service.has_due(["a"], NOW + timedelta(minutes=15))
    assert service.dispatch_due(["a"], NOW + timedelta(minutes=15)) == []
    assert len(chat.calls) == 1
    store.close()
