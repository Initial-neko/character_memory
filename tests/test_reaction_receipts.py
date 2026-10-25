"""W2 B1: model work is claimed once and cognition commits with its receipt."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import json

import pytest

from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, MemoryCandidate, PersonReaction
from character_memory.llm.client import ModelCallResult, ModelCallTrace, PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore

NOW = datetime(2026, 10, 25, 11, 0, tzinfo=timezone.utc)


class CountedModel(PersonModel):
    def __init__(self, *, fail=False, entered=None, release=None):
        self.calls = 0
        self.fail, self.entered, self.release = fail, entered, release
        self.lock = threading.Lock()

    def react(self, context):
        return self.react_call_for_session(context, "fixture").value

    def react_call_for_session(self, context, session_id):
        with self.lock:
            self.calls += 1
            count = self.calls
        if self.entered is not None and count == 1:
            self.entered.set()
            assert self.release.wait(10), "fixture synchronization failed"
        if self.fail:
            raise RuntimeError("provider response was lost")
        value = PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="真实提交一次")],
            mental_state_update="记得本轮互动", memory_candidates=[MemoryCandidate(content="一次共同经历", importance=.8)])
        return ModelCallResult(value=value, trace=ModelCallTrace(request_messages=[], response_text='{"actions":[]}', attempt=1, model="counted-local-fixture"))


def runtime(store, model):
    emb = DeterministicEmbedding()
    return PersonRuntime(store, VectorRecall(store, emb), emb, model, "fixture persona")


def source(store, *, character_id="momo"):
    return store.append_event(Event(character_id=character_id, event_type=EventType.USER_MESSAGE,
        event_time=NOW, content="同一来源", metadata={"conversation_id": "once"}))


def snapshot(store):
    return {table: store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("events", "memories", "intents", "runtime_traces", "mental_state_history")}


def group_service(store, models):
    return GroupConversationService(store, {key: runtime(store, model) for key, model in models.items()},
        FixedClock(NOW), profiles=[{"id": key, "name": key} for key in models])


def test_direct_replay_and_restart_reuse_committed_result_without_provider_or_writes(tmp_path):
    path = tmp_path / "direct.db"
    store = SQLiteStore(path)
    model = CountedModel()
    person = runtime(store, model)
    event = source(store)
    first = person.handle(event, persist_event=False)
    before = snapshot(store)
    second = person.handle(event, persist_event=False)
    assert model.calls == 1
    assert second.replayed is True
    assert second.created_memory_ids == first.created_memory_ids
    assert snapshot(store) == before
    store.close()
    store = SQLiteStore(path)
    fresh = CountedModel()
    result = runtime(store, fresh).handle(event, persist_event=False)
    assert result.replayed and fresh.calls == 0
    assert snapshot(store) == before
    store.close()


def test_group_replay_reuses_each_persons_commit(tmp_path):
    store = SQLiteStore(tmp_path / "group.db")
    models = {name: CountedModel() for name in ("momo", "rin")}
    service = group_service(store, models)
    group = service.create_group("once", list(models), at=NOW)
    event = service.persist_user_event(group.id, "共同来源", at=NOW)
    first = service.react_from_event(event)
    count = store.conn.execute("SELECT COUNT(*) FROM conversation_events").fetchone()[0]
    second = service.react_from_event(event)
    assert [model.calls for model in models.values()] == [1, 1]
    assert all(row["replayed"] for row in second["decisions"])
    assert [r["created_memory_ids"] for r in first["decisions"]] == [r["created_memory_ids"] for r in second["decisions"]]
    assert store.conn.execute("SELECT COUNT(*) FROM conversation_events").fetchone()[0] == count
    store.close()


def test_provider_unknown_is_not_called_again_on_same_source(tmp_path):
    store = SQLiteStore(tmp_path / "unknown.db")
    model = CountedModel(fail=True)
    person = runtime(store, model)
    event = source(store)
    with pytest.raises(RuntimeError, match="response was lost"):
        person.handle(event, persist_event=False)
    with pytest.raises(RuntimeError):
        person.handle(event, persist_event=False)
    assert model.calls == 1
    assert store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    store.close()


def test_partial_group_failure_does_not_repeat_failed_or_completed_members(tmp_path):
    store = SQLiteStore(tmp_path / "partial.db")
    bad, good = CountedModel(fail=True), CountedModel()
    service = group_service(store, {"momo": bad, "rin": good})
    group = service.create_group("partial", ["momo", "rin"], at=NOW)
    event = service.persist_user_event(group.id, "一次", at=NOW)
    one = service.react_from_event(event)
    two = service.react_from_event(event)
    assert bad.calls == good.calls == 1
    assert len([row for row in one["decisions"] if row.get("error")]) == 1
    assert len([row for row in two["decisions"] if row.get("error")]) == 1
    store.close()


def test_derived_transaction_failure_rolls_back_and_does_not_repeat_provider(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "rollback.db")
    model = CountedModel()
    person = runtime(store, model)
    event = source(store)
    before = snapshot(store)
    original = store.add_runtime_trace
    def broken(*args):
        raise RuntimeError("injected commit failure")
    monkeypatch.setattr(store, "add_runtime_trace", broken)
    with pytest.raises(RuntimeError, match="commit failure"):
        person.handle(event, persist_event=False)
    assert snapshot(store) == before
    monkeypatch.setattr(store, "add_runtime_trace", original)
    with pytest.raises(RuntimeError):
        person.handle(event, persist_event=False)
    assert model.calls == 1
    store.close()


def test_two_connections_cannot_enter_provider_for_one_source(tmp_path):
    path = tmp_path / "concurrent.db"
    first, second = SQLiteStore(path), SQLiteStore(path)
    entered, release = threading.Event(), threading.Event()
    model = CountedModel(entered=entered, release=release)
    one, two = runtime(first, model), runtime(second, model)
    event = source(first)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(one.handle, event, persist_event=False)
        assert entered.wait(5)
        try:
            with pytest.raises(RuntimeError):
                two.handle(event, persist_event=False)
        finally:
            release.set()
        assert result.result(timeout=10).created_memory_ids
    assert model.calls == 1
    first.close(); second.close()


def test_direct_source_owner_is_checked_before_provider(tmp_path):
    store = SQLiteStore(tmp_path / "owner.db")
    model = CountedModel()
    event = source(store, character_id="rin")
    with pytest.raises(ValueError):
        runtime(store, model).handle(event.model_copy(update={"character_id": "momo"}), persist_event=False)
    assert model.calls == 0
    store.close()


def test_corrupt_group_result_is_not_reported_as_success(tmp_path):
    store = SQLiteStore(tmp_path / "corrupt-group.db")
    models = {name: CountedModel() for name in ("momo", "rin")}
    service = group_service(store, models)
    group = service.create_group("corrupt", list(models), at=NOW)
    event = service.persist_user_event(group.id, "fact", at=NOW)
    service.react_from_event(event)
    store.conn.execute("UPDATE conversation_reaction_receipts SET result_json='{}' WHERE character_id='momo'")
    store.conn.commit()
    result = service.react_from_event(event)
    bad = next(item for item in result["decisions"] if item["character_id"] == "momo")
    assert bad.get("error"), "corrupt cognition must not become successful silence"
    assert [model.calls for model in models.values()] == [1, 1]
    store.close()


@pytest.mark.parametrize("corruption", ["owner", "conversation"])
def test_cached_direct_result_cannot_change_the_source_owner(tmp_path, corruption):
    import json
    store = SQLiteStore(tmp_path / "corrupt-owner.db")
    model = CountedModel()
    person = runtime(store, model)
    event = source(store)
    person.handle(event, persist_event=False)
    row = store.conn.execute("SELECT result_json FROM conversation_reaction_receipts").fetchone()
    result = json.loads(row[0])
    if corruption == "owner":
        result["event"]["character_id"] = "rin"
    else:
        result["event"]["metadata"]["conversation_id"] = "another-conversation"
    store.conn.execute("UPDATE conversation_reaction_receipts SET result_json=?", (json.dumps(result),))
    store.conn.commit()
    with pytest.raises(RuntimeError):
        person.handle(event, persist_event=False)
    assert model.calls == 1
    store.close()


def test_oversized_source_identity_is_rejected_before_sqlite_or_provider(tmp_path):
    store = SQLiteStore(tmp_path / "large-source.db")
    model = CountedModel()
    event = source(store).model_copy(update={"id": 2**64})
    with pytest.raises(ValueError):
        runtime(store, model).handle(event, persist_event=False)
    assert model.calls == 0
    store.close()


def test_failure_before_model_can_be_explicitly_retried(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "before-model.db")
    model = CountedModel()
    person = runtime(store, model)
    event = source(store)
    original = person.context_builder.build
    def unavailable(*args, **kwargs):
        raise RuntimeError("context unavailable before provider")
    monkeypatch.setattr(person.context_builder, "build", unavailable)
    with pytest.raises(RuntimeError, match="context unavailable"):
        person.handle(event, persist_event=False)
    assert model.calls == 0
    assert store.conn.execute("SELECT state FROM conversation_reaction_receipts").fetchone()[0] == "FAILED_BEFORE_CALL"
    monkeypatch.setattr(person.context_builder, "build", original)
    assert person.handle(event, persist_event=False).created_memory_ids
    assert model.calls == 1
    store.close()


def test_commit_guard_stops_stale_work_before_provider(tmp_path):
    from character_memory.runtime.person_runtime import SupersededReaction
    store = SQLiteStore(tmp_path / "stale.db")
    model = CountedModel()
    person = runtime(store, model)
    event = source(store)
    checks = iter([True, False])
    with pytest.raises(SupersededReaction):
        person.handle(event, persist_event=False, commit_guard=lambda: next(checks))
    assert model.calls == 0
    assert store.conn.execute("SELECT state FROM conversation_reaction_receipts").fetchone()[0] == "SUPERSEDED"
    store.close()


def test_lost_execution_owner_cannot_commit_derived_effects(tmp_path):
    store = SQLiteStore(tmp_path / "lost-owner.db")
    model = CountedModel()
    person = runtime(store, model)
    event = source(store)
    before = snapshot(store)
    original = model.react_call_for_session
    def changed_owner(context, session):
        result = original(context, session)
        with store.transaction(immediate=True):
            store.conn.execute("UPDATE conversation_reaction_receipts SET owner_token='another-owner'")
        return result
    model.react_call_for_session = changed_owner
    with pytest.raises(RuntimeError, match="LOST_OWNER"):
        person.handle(event, persist_event=False)
    assert snapshot(store) == before
    assert model.calls == 1
    store.close()


def test_legacy_trace_without_new_receipt_is_not_reexecuted(tmp_path):
    store = SQLiteStore(tmp_path / "legacy.db")
    model = CountedModel()
    person = runtime(store, model)
    event = source(store)
    store.add_runtime_trace("momo", event.id, NOW, {"source_event_id": event.id, "actions": []})
    with pytest.raises(RuntimeError, match="LEGACY_TRACE_REQUIRES_RECONCILIATION"):
        person.handle(event, persist_event=False)
    assert model.calls == 0
    store.close()


def test_equal_core_and_group_ids_keep_distinct_cognition_receipts(tmp_path):
    store = SQLiteStore(tmp_path / "namespaces.db")
    momo, rin = CountedModel(), CountedModel()
    person = runtime(store, momo)
    event = source(store)
    person.handle(event, persist_event=False)
    service = GroupConversationService(store, {"momo": person, "rin": runtime(store, rin)}, FixedClock(NOW))
    group = service.create_group("distinct", ["momo", "rin"], at=NOW)
    shared = service.persist_user_event(group.id, "shared", at=NOW)
    assert shared.id == event.id
    service.react_from_event(shared)
    person.handle(event, persist_event=False)
    service.react_from_event(shared)
    assert momo.calls == 2 and rin.calls == 1
    assert store.conn.execute("SELECT COUNT(*) FROM conversation_reaction_receipts WHERE state='COMMITTED'").fetchone()[0] == 3
    store.close()


@pytest.mark.parametrize("expired_preparation", [False, True])
def test_two_real_processes_cannot_call_provider_twice_for_one_fact(tmp_path, expired_preparation):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time
    path = tmp_path / "processes.db"
    store = SQLiteStore(path)
    event = source(store)
    if expired_preparation:
        from character_memory.domain.facts import FactRef
        from character_memory.storage.message_receipts import ReactionExecutionRepository
        ReactionExecutionRepository(store, clock=lambda: time.time() - 121).claim(
            FactRef(kind="EVENT", id=event.id), "momo")
    marker = tmp_path / "provider-calls.txt"
    script = """
import json,sys,time
from character_memory.storage.sqlite import SQLiteStore
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.llm.client import PersonModel,ModelCallResult,ModelCallTrace
from character_memory.domain.models import PersonReaction,ActionDecision
from character_memory.storage.message_receipts import ExecutionUnavailable
class Model(PersonModel):
 def react(self,context): return self.react_call_for_session(context,'fixture').value
 def react_call_for_session(self,context,session):
  with open(sys.argv[3],'a',encoding='utf-8') as output: output.write('CALL\\n')
  time.sleep(.3)
  return ModelCallResult(PersonReaction(actions=[ActionDecision(type='MESSAGE',message='once')]),ModelCallTrace(request_messages=[],response_text='{}',attempt=1,model='process-local-fixture'))
store=SQLiteStore(sys.argv[1]);emb=DeterministicEmbedding();person=PersonRuntime(store,VectorRecall(store,emb),emb,Model(),'fixture')
event=store.get_event(int(sys.argv[2]))
while time.time()<float(sys.argv[4]): time.sleep(.01)
try:
 result=person.handle(event,persist_event=False)
 print(json.dumps({'status':'COMMITTED','replayed':result.replayed}))
except ExecutionUnavailable as exc: print(json.dumps({'status':exc.state}))
finally: store.close()
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONIOENCODING": "utf-8"}
    command = [sys.executable, "-c", script, str(path), str(event.id), str(marker), str(time.time() + 3)]
    processes = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for _ in range(2)]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
    assert marker.read_text(encoding="utf-8").splitlines() == ["CALL"]
    assert any(result["status"] == "COMMITTED" for result in results)
    assert store.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='CHARACTER_MESSAGE'").fetchone()[0] == 1
    store.close()



def test_caller_transaction_cannot_hide_durable_execution_boundary(tmp_path):
    store = SQLiteStore(tmp_path / "outer-transaction.db")
    model = CountedModel()
    person = runtime(store, model)
    event = source(store)
    with store.transaction():
        with pytest.raises(RuntimeError, match="caller transaction"):
            person.handle(event, persist_event=False)
    assert model.calls == 0
    assert person.handle(event, persist_event=False).created_memory_ids
    assert model.calls == 1
    store.close()


def test_group_caller_transaction_is_rejected_before_member_models(tmp_path):
    store = SQLiteStore(tmp_path / "group-outer.db")
    models = {name: CountedModel() for name in ("momo", "rin")}
    service = group_service(store, models)
    group = service.create_group("outer", list(models), at=NOW)
    event = service.persist_user_event(group.id, "once", at=NOW)
    with store.transaction():
        with pytest.raises(RuntimeError, match="caller transaction"):
            service.react_from_event(event)
    assert all(model.calls == 0 for model in models.values())
    assert len(service.react_from_event(event)["decisions"]) == 2
    assert all(model.calls == 1 for model in models.values())
    store.close()


def test_other_threads_transaction_does_not_look_like_callers_transaction(tmp_path):
    from concurrent.futures import TimeoutError
    store = SQLiteStore(tmp_path / "other-owner.db")
    person = runtime(store, CountedModel())
    event = source(store)
    entered, release, attempting = threading.Event(), threading.Event(), threading.Event()
    def hold():
        with store.transaction():
            entered.set()
            assert release.wait(5)
    def react():
        attempting.set()
        return person.handle(event, persist_event=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(hold)
        assert entered.wait(5)
        result = pool.submit(react)
        assert attempting.wait(5)
        try:
            with pytest.raises(TimeoutError):
                result.result(timeout=.1)
        finally:
            release.set()
        held.result(timeout=5)
        assert result.result(timeout=5).created_memory_ids
    store.close()

# B2a: only an explicitly expired pre-model preparation can be reclaimed.
def receipt_owner(store, now):
    from character_memory.storage.message_receipts import ReactionExecutionRepository
    return ReactionExecutionRepository(store, preparation_lease_seconds=10, clock=lambda: now[0])


def receipt_source(store):
    from character_memory.domain.facts import FactRef
    return FactRef(kind="EVENT", id=source(store).id)


def test_expired_preparation_changes_owner_and_fences_old_worker(tmp_path):
    from character_memory.storage.message_receipts import ExecutionUnavailable
    store = SQLiteStore(tmp_path / "lease.db")
    now = [100.0]
    first_repo = receipt_owner(store, now)
    ref = receipt_source(store)
    old = first_repo.claim(ref, "momo")
    other = SQLiteStore(tmp_path / "lease.db")
    second_repo = receipt_owner(other, now)
    with pytest.raises(ExecutionUnavailable, match="PREPARING"):
        second_repo.claim(ref, "momo")
    now[0] = 110.0
    new = second_repo.claim(ref, "momo")
    assert old.token != new.token
    with pytest.raises(ExecutionUnavailable, match="LOST_OWNER"):
        first_repo.begin_model(old)
    first_repo.fail(old, RuntimeError("stale worker"))
    second_repo.begin_model(new)
    with pytest.raises(ExecutionUnavailable, match="LOST_OWNER"):
        with store.transaction():
            first_repo.commit(old, {"old": True})
    with other.transaction():
        second_repo.commit(new, {"new": True})
    assert second_repo.claim(ref, "momo").result == {"new": True}
    other.close()
    store.close()


def test_expired_original_owner_cannot_start_even_without_a_takeover(tmp_path):
    from character_memory.storage.message_receipts import ExecutionUnavailable
    store = SQLiteStore(tmp_path / "late.db")
    now = [100.0]
    repo = receipt_owner(store, now)
    work = repo.claim(receipt_source(store), "momo")
    now[0] = 110.0
    with pytest.raises(ExecutionUnavailable, match="LOST_OWNER"):
        repo.begin_model(work)
    store.close()


@pytest.mark.parametrize("state", ["CALLING", "UNKNOWN", "SUPERSEDED"])
def test_expiry_never_reclaims_known_call_or_terminal_state(tmp_path, state):
    from character_memory.storage.message_receipts import ExecutionUnavailable
    store = SQLiteStore(tmp_path / "unknown.db")
    now = [100.0]
    repo = receipt_owner(store, now)
    ref = receipt_source(store)
    work = repo.claim(ref, "momo")
    repo.begin_model(work)
    if state != "CALLING":
        repo.fail(work, RuntimeError("provider interrupted"), superseded=state == "SUPERSEDED")
    now[0] = 10000.0
    with pytest.raises(ExecutionUnavailable, match=state):
        repo.claim(ref, "momo")
    store.close()


def test_legacy_preparation_without_deadline_requires_reconciliation(tmp_path):
    from character_memory.storage.message_receipts import ExecutionUnavailable
    store = SQLiteStore(tmp_path / "legacy-lease.db")
    now = [100.0]
    repo = receipt_owner(store, now)
    ref = receipt_source(store)
    repo.claim(ref, "momo")
    with store.transaction():
        store.conn.execute("UPDATE conversation_reaction_receipts SET preparation_deadline=NULL")
    now[0] = 10000.0
    with pytest.raises(ExecutionUnavailable, match="PREPARING"):
        repo.claim(ref, "momo")
    store.close()


def test_server_clock_rollback_does_not_reclaim_preparation(tmp_path):
    from character_memory.storage.message_receipts import ExecutionUnavailable
    store = SQLiteStore(tmp_path / "clock.db")
    now = [100.0]
    repo = receipt_owner(store, now)
    ref = receipt_source(store)
    repo.claim(ref, "momo")
    now[0] = 50.0
    with pytest.raises(ExecutionUnavailable, match="PREPARING"):
        repo.claim(ref, "momo")
    store.close()


def test_preparation_lease_has_formal_bounds_and_help():
    from character_memory.config import Settings
    from pydantic import ValidationError
    field = Settings.model_fields["reaction_preparation_lease_seconds"]
    assert field.description
    assert Settings().reaction_preparation_lease_seconds == 120.0
    for invalid in (0, -1, 3601, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Settings(reaction_preparation_lease_seconds=invalid)


def test_group_recovers_only_expired_preparation_then_reuses_commit(tmp_path):
    from character_memory.domain.facts import FactRef
    store = SQLiteStore(tmp_path / "group-lease.db")
    model = CountedModel()
    service = group_service(store, {"momo": model, "rin": CountedModel()})
    group = service.create_group("recover", ["momo", "rin"], at=NOW)
    event = service.persist_user_event(group.id, "recover once", at=NOW)
    now = [100.0]
    repo = receipt_owner(store, now)
    ref = FactRef(kind="CONVERSATION_EVENT", id=event.id, conversation_id=group.id)
    repo.claim(ref, "momo")
    service.runtimes["momo"]._execution_receipts = repo
    now[0] = 111.0
    result = service.react_from_event(event)
    replay = service.react_from_event(event)
    assert model.calls == 1
    assert len(result["decisions"]) == 2
    assert replay["decisions"][0]["replayed"] is True
    store.close()


def test_preparation_migration_preserves_published_receipts_without_inventing_a_deadline(tmp_path):
    store = SQLiteStore(tmp_path / "published-schema.db")
    ref = receipt_source(store)
    def old_schema():
        store.conn.execute("CREATE TABLE conversation_reaction_receipts (source_ref_json TEXT NOT NULL, "
            "character_id TEXT NOT NULL, reaction_version TEXT NOT NULL, state TEXT NOT NULL, "
            "owner_token TEXT NOT NULL, result_json TEXT, error_kind TEXT, "
            "PRIMARY KEY(source_ref_json,character_id,reaction_version))")
        store.conn.execute("INSERT INTO conversation_reaction_receipts VALUES(?,?,?,?,?,?,?)",
            (ref.model_dump_json(), "momo", "1", "PREPARING", "published-owner", None, None))
    store.apply_schema_migration("conversation/002-reaction-execution", old_schema, immediate=True)
    receipt_owner(store, [10000.0])
    row = store.conn.execute("SELECT * FROM conversation_reaction_receipts").fetchone()
    assert row["owner_token"] == "published-owner"
    assert row["state"] == "PREPARING"
    assert row["preparation_deadline"] is None
    # Opening another runtime must not repeat ALTER TABLE or change old ownership.
    receipt_owner(store, [10001.0])
    assert store.conn.execute("SELECT COUNT(*) FROM conversation_reaction_receipts").fetchone()[0] == 1
    store.close()
