"""P08 acceptance: evidence watermarks and daily/weekly/monthly reflection."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from character_memory.application.reflection_service import (
    POLICY_VERSION,
    ReflectionService,
    reflection_window,
)
from character_memory.domain.facts import FactRef
from character_memory.domain.goals import GoalAction, GoalCreate
from character_memory.domain.models import Event, EventType, Memory
from character_memory.llm.usage import current_llm_usage_context, infer_usage_context
from character_memory.reflection_runtime import ReflectionScheduler
from character_memory.reflection_web import ReflectionReader, attach_reflection_routes
from character_memory.storage.person_goals import GoalRepository
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)
OFFSET = 480


def _settings(**overrides):
    values = {
        "reflection_enabled": True,
        "reflection_offset_minutes": OFFSET,
        "autonomous_model_daily_max": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeReflectionModel:
    def __init__(self, *, refs=None, mode="auto"):
        self.calls = []
        self.scopes = []
        self.refs = refs
        self.mode = mode

    def structured_for_session(self, prompt, schema, session_id):
        self.calls.append((schema.__name__, session_id, prompt))
        self.scopes.append(current_llm_usage_context())
        if self.mode == "forbidden":
            return {"summary": "越界输出", "evidence_refs": [], "actions": [{"type": "POST"}],
                    "intent_candidates": [], "goal_status": "COMPLETED"}
        if self.mode == "oversize":
            return schema(summary="x", evidence_refs=[], open_questions=["q" * 1000] * 100)
        refs = list(self.refs) if self.refs is not None else self._refs_from_prompt(prompt)
        if schema.__name__ == "ReflectionDailyOutput":
            return schema(summary="这天我整理了自己的经历。", evidence_refs=refs, open_questions=[])
        if schema.__name__ == "ReflectionWeeklyOutput":
            return schema(patterns=["重复出现的关注点"], changes=["有一点变化"], evidence_refs=refs)
        if schema.__name__ == "ReflectionMonthlyOutput":
            return schema(drifts=["缓慢的变化"], conflicts=[], evidence_refs=refs)
        raise AssertionError(f"unexpected schema: {schema.__name__}")

    @staticmethod
    def _refs_from_prompt(prompt):
        refs = []
        for event_id in re.findall(r"\[EVENT (\d+)\]", prompt):
            refs.append(FactRef(kind="EVENT", id=int(event_id)))
        for event_id, conversation_id in re.findall(
            r"\[CONVERSATION_EVENT (\d+) conversation=([^\]]+)\]", prompt
        ):
            refs.append(FactRef(kind="CONVERSATION_EVENT", id=int(event_id),
                                conversation_id=conversation_id))
        return refs[:1]


def make_service(tmp_path, *, model=None, clock=NOW, path=None, **settings):
    store_path = path or (tmp_path / "reflection.db")
    store = SQLiteStore(store_path)
    fake = model or FakeReflectionModel()
    service = ReflectionService(
        store, _settings(**settings), model_provider=lambda: fake, clock=lambda: clock,
    )
    return store, service, fake


def seed_event(store, at, *, character="c00", content="真实经历", event_type=EventType.WORLD_OBSERVATION,
               metadata=None):
    return store.append_event(Event(
        character_id=character, event_type=event_type, event_time=at,
        content=content, metadata=dict(metadata or {}),
    ))


def period_count(store):
    return store.conn.execute("SELECT COUNT(*) FROM reflection_periods").fetchone()[0]


def receipt_count(store):
    return store.conn.execute("SELECT COUNT(*) FROM model_invocation_receipts").fetchone()[0]


def get_period(store, kind="DAILY", character="c00"):
    rows = store.conn.execute(
        "SELECT * FROM reflection_periods WHERE character_id=? AND kind=? ORDER BY window_start DESC",
        (character, kind),
    ).fetchall()
    return rows[0] if rows else None


# A1 -------------------------------------------------------------------------
def test_a1_idle_window_has_zero_calls_and_one_skipped_period(tmp_path):
    store, service, model = make_service(tmp_path)
    try:
        result = service.run_period("c00", "DAILY", now=NOW)
        assert result["status"] == "SKIPPED_IDLE"
        assert result["reason"] == "IDLE_NO_NEW_EVIDENCE"
        assert model.calls == []
        assert receipt_count(store) == 0
        assert period_count(store) == 1
        assert store.conn.execute("SELECT COUNT(*) FROM reflection_watermarks").fetchone()[0] == 0
    finally:
        store.close()


# A2 -------------------------------------------------------------------------
def test_a2_repeated_period_is_byte_identical_and_free(tmp_path):
    store, service, model = make_service(tmp_path)
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store, window.start + timedelta(hours=1))
    try:
        first = service.run_period("c00", "DAILY", now=NOW)
        assert first["status"] == "COMMITTED"
        second = service.run_period("c00", "DAILY", now=NOW)
        assert second["reason"] == "PERIOD_ALREADY_COMMITTED"
        assert json.dumps(first["output"], sort_keys=True) == json.dumps(second["output"], sort_keys=True)
        assert len(model.calls) == 1
        assert period_count(store) == 1
    finally:
        store.close()


# A3 -------------------------------------------------------------------------
def test_a3_restart_replays_success_receipt_and_commits_without_paying(tmp_path):
    path = tmp_path / "restart.db"
    store, service, model = make_service(tmp_path, path=path)
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store, window.start + timedelta(hours=1))
    first = service.run_period("c00", "DAILY", now=NOW)
    assert first["status"] == "COMMITTED" and len(model.calls) == 1
    # Simulate a crash after the paid receipt committed but before the period did.
    store.conn.execute(
        "UPDATE reflection_periods SET status='RUNNING',output_json=NULL,completed_at=NULL",
    )
    store.conn.execute("DELETE FROM reflection_watermarks")
    store.conn.commit()
    store.close()

    store2 = SQLiteStore(path)
    model2 = FakeReflectionModel()
    service2 = ReflectionService(
        store2, _settings(), model_provider=lambda: model2, clock=lambda: NOW,
    )
    try:
        resumed = service2.run_period("c00", "DAILY", now=NOW)
        assert resumed["status"] == "COMMITTED"
        assert resumed["output"] == first["output"]
        assert model2.calls == []
        assert receipt_count(store2) == 1
    finally:
        store2.close()


# A4 -------------------------------------------------------------------------
def test_a4_future_event_does_not_trigger_or_advance_cursor(tmp_path):
    store, service, model = make_service(tmp_path)
    future = seed_event(store, NOW + timedelta(days=1), content="还没有发生")
    try:
        first = service.run_period("c00", "DAILY", now=NOW)
        assert first["status"] == "SKIPPED_IDLE" and model.calls == []
        assert service.repository.get_watermark("c00", "DAILY") is None
        later = service.run_period("c00", "DAILY", now=NOW + timedelta(days=2))
        assert later["status"] == "COMMITTED" and len(model.calls) == 1
        watermark = service.repository.get_watermark("c00", "DAILY")
        assert watermark["event_cursor"] == future.id
    finally:
        store.close()


# A5 -------------------------------------------------------------------------
def test_a5_correction_triggers_next_period_once(tmp_path):
    store, service, model = make_service(tmp_path)
    first_window = reflection_window("DAILY", NOW, OFFSET)
    old = store.add_memory(Memory(
        character_id="c00", content="旧的理解", event_time=first_window.start + timedelta(hours=1),
        memory_type="EPISODIC", importance=0.6,
    ))
    try:
        assert service.run_period("c00", "DAILY", now=NOW)["status"] == "COMMITTED"
        assert len(model.calls) == 1
        next_window = reflection_window("DAILY", NOW + timedelta(days=1), OFFSET)
        replacement = Memory(
            character_id="c00", content="更正后的理解",
            event_time=next_window.start + timedelta(hours=1), memory_type="EPISODIC",
            importance=0.8, metadata={"governance": "CORRECTED", "corrects_memory_id": old.id},
        )
        store.supersede_memory(old.id, replacement)
        changed = service.run_period("c00", "DAILY", now=NOW + timedelta(days=1))
        assert changed["status"] == "COMMITTED"
        assert len(model.calls) == 2
        again = service.run_period("c00", "DAILY", now=NOW + timedelta(days=1))
        assert again["reason"] == "PERIOD_ALREADY_COMMITTED"
        assert len(model.calls) == 2
    finally:
        store.close()


# A6 -------------------------------------------------------------------------
def test_a6_forget_and_restore_each_trigger_without_new_rows(tmp_path):
    store, service, model = make_service(tmp_path)
    window = reflection_window("DAILY", NOW, OFFSET)
    memory = store.add_memory(Memory(
        character_id="c00", content="一条旧记忆", event_time=window.start + timedelta(hours=1),
        memory_type="EPISODIC",
    ))
    try:
        assert service.run_period("c00", "DAILY", now=NOW)["status"] == "COMMITTED"
        assert len(model.calls) == 1
        store.set_memory_active(memory.id, False)
        forgotten = service.run_period("c00", "DAILY", now=NOW + timedelta(days=1))
        assert forgotten["status"] == "COMMITTED" and len(model.calls) == 2
        store.set_memory_active(memory.id, True)
        restored = service.run_period("c00", "DAILY", now=NOW + timedelta(days=2))
        assert restored["status"] == "COMMITTED" and len(model.calls) == 3
        assert store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    finally:
        store.close()


# A7 -------------------------------------------------------------------------
def test_a7_concurrent_claim_allows_exactly_one_provider_call(tmp_path):
    path = tmp_path / "concurrent.db"
    started = threading.Event()
    release = threading.Event()

    class BlockingModel(FakeReflectionModel):
        def structured_for_session(self, prompt, schema, session_id):
            started.set()
            assert release.wait(10)
            return super().structured_for_session(prompt, schema, session_id)

    store_a, service_a, model_a = make_service(tmp_path, model=BlockingModel(), path=path)
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store_a, window.start + timedelta(hours=1))
    results = {}

    def run_winner():
        results["winner"] = service_a.run_period("c00", "DAILY", now=NOW)

    thread = threading.Thread(target=run_winner)
    thread.start()
    try:
        assert started.wait(10)
        store_b = SQLiteStore(path)
        model_b = FakeReflectionModel()
        service_b = ReflectionService(
            store_b, _settings(), model_provider=lambda: model_b, clock=lambda: NOW,
        )
        loser = service_b.run_period("c00", "DAILY", now=NOW)
        assert model_b.calls == []
        assert loser["status"] in {"RUNNING", "UNKNOWN"}
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert results["winner"]["status"] == "COMMITTED"
        assert len(model_a.calls) == 1
        assert period_count(store_a) == 1
        store_b.close()
    finally:
        release.set()
        thread.join(10)
        store_a.close()


# A8 -------------------------------------------------------------------------
def test_a8_zero_budget_blocks_without_watermark_advance(tmp_path):
    store, service, model = make_service(tmp_path, autonomous_model_daily_max=0)
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store, window.start + timedelta(hours=1))
    try:
        result = service.run_period("c00", "DAILY", now=NOW)
        assert result["status"] == "BLOCKED_BUDGET"
        assert result["reason"] == "BUDGET_EXHAUSTED"
        assert model.calls == []
        assert service.repository.get_watermark("c00", "DAILY") is None
        assert receipt_count(store) == 0
    finally:
        store.close()


# A9 -------------------------------------------------------------------------
def test_a9_one_budget_slot_runs_daily_then_blocks_weekly_monthly(tmp_path):
    store, service, model = make_service(tmp_path, autonomous_model_daily_max=1)
    for kind in ("DAILY", "WEEKLY", "MONTHLY"):
        window = reflection_window(kind, NOW, OFFSET)
        seed_event(store, window.start + timedelta(hours=1), content=f"{kind} 证据", event_type=EventType.SPACE_POST_SEEN)
    try:
        results = service.run_all(["c00"], now=NOW)
        by_kind = {item["kind"]: item for item in results}
        assert by_kind["DAILY"]["status"] == "COMMITTED"
        assert by_kind["WEEKLY"]["status"] == "BLOCKED_BUDGET"
        assert by_kind["MONTHLY"]["status"] == "BLOCKED_BUDGET"
        assert len(model.calls) == 1
        assert receipt_count(store) == 1
    finally:
        store.close()


def test_blocked_budget_period_retries_after_utc_budget_day_resets(tmp_path):
    path = tmp_path / "blocked-retry.db"
    store, first, first_model = make_service(tmp_path, path=path, autonomous_model_daily_max=1)
    daily = reflection_window("DAILY", NOW, OFFSET)
    weekly = reflection_window("WEEKLY", NOW, OFFSET)
    seed_event(store, daily.start + timedelta(hours=1), content="日证据")
    seed_event(store, weekly.start + timedelta(hours=1), content="周证据",
               event_type=EventType.SPACE_POST_SEEN)
    try:
        assert first.run_period("c00", "DAILY", now=NOW)["status"] == "COMMITTED"
        blocked = first.run_period("c00", "WEEKLY", now=NOW)
        assert blocked["status"] == "BLOCKED_BUDGET"
        # A later UTC budget day is a new slot; the same closed week may retry.
        second_model = FakeReflectionModel()
        second = ReflectionService(
            store, _settings(autonomous_model_daily_max=1),
            model_provider=lambda: second_model, clock=lambda: NOW + timedelta(days=1),
        )
        retried = second.run_period("c00", "WEEKLY", now=NOW + timedelta(days=1))
        assert retried["status"] == "COMMITTED"
        assert len(second_model.calls) == 1
        assert len(first_model.calls) == 1  # only the daily call that used the slot
    finally:
        store.close()


# A10 ------------------------------------------------------------------------
def _table_count(store, table):
    exists = store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if exists else 0


def test_a10_reflection_does_not_write_memory_intent_goal_or_action(tmp_path):
    store, service, model = make_service(tmp_path)
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store, window.start + timedelta(hours=1))
    tables = ("memories", "intents", "mental_states", "person_goals", "conversation_events", "events")
    before = {table: _table_count(store, table) for table in tables}
    try:
        assert service.run_period("c00", "DAILY", now=NOW)["status"] == "COMMITTED"
        after = {table: _table_count(store, table) for table in tables}
        assert after == before
        assert store.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='ACTION'"
        ).fetchone()[0] == 0
    finally:
        store.close()


# A11 ------------------------------------------------------------------------
def test_a11_forbidden_action_or_intent_fields_are_rejected_whole(tmp_path):
    store, service, model = make_service(tmp_path, model=FakeReflectionModel(mode="forbidden"))
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store, window.start + timedelta(hours=1))
    try:
        result = service.run_period("c00", "DAILY", now=NOW)
        assert result["status"] == "FAILED" and result["output"] is None
        assert _table_count(store, "intents") == 0
        assert store.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='ACTION'"
        ).fetchone()[0] == 0
    finally:
        store.close()


# A12 ------------------------------------------------------------------------
def test_a12_goal_completion_still_requires_operator_and_fact_ref(tmp_path):
    store = SQLiteStore(tmp_path / "goals.db")
    repo = GoalRepository(store)
    try:
        created = repo.create("c00", GoalCreate(
            request_id="create-goal-1", title="长期目标", success_criteria="完成一件事",
            stages=["第一步", "第二步"],
        ))
        goal_id = created["goal"]["id"]
        with pytest.raises(ValidationError):
            GoalAction(request_id="action-001", expected_version=1, operation="COMPLETE_STAGE")
        with pytest.raises(ValidationError):
            GoalAction(
                request_id="action-002", expected_version=1, operation="COMPLETE_STAGE",
                confirmation="OPERATOR_ACCEPTED", evidence=[],
            )
        with pytest.raises(ValidationError):
            FactRef.model_validate({"kind": "REFLECTION", "id": 1})
        # A reflection period has no FactRef kind, so it can never be submitted
        # as stage evidence; the operator API still refuses without a real ref.
        assert repo.get("c00", goal_id)["current_stage"] == 0
    finally:
        store.close()


# A13 ------------------------------------------------------------------------
def test_a13_output_refs_must_resolve_and_simulated_sources_are_excluded(tmp_path):
    store, service, model = make_service(tmp_path)
    window = reflection_window("DAILY", NOW, OFFSET)
    valid = seed_event(store, window.start + timedelta(hours=1), content="真实经历")
    simulated_meta = {"experience_scope": {"schema_version": 1, "reality": "SIMULATED"}}
    simulated = seed_event(store, window.start + timedelta(hours=2), content="模拟经历",
                           metadata=simulated_meta)
    try:
        model.refs = [FactRef(kind="EVENT", id=valid.id)]
        committed = service.run_period("c00", "DAILY", now=NOW)
        assert committed["status"] == "COMMITTED"
        assert [item["id"] for item in committed["evidence"]["facts"]] == [valid.id]
        assert simulated.id not in [item["id"] for item in committed["evidence"]["facts"]]

        # A ref that was not part of this window's evidence is rejected as a whole.
        next_window = reflection_window("DAILY", NOW + timedelta(days=1), OFFSET)
        seed_event(store, next_window.start + timedelta(hours=1), content="下一天")
        model.refs = [FactRef(kind="EVENT", id=simulated.id)]
        rejected = service.run_period("c00", "DAILY", now=NOW + timedelta(days=1))
        assert rejected["status"] == "FAILED" and rejected["output"] is None
    finally:
        store.close()


# A14 ------------------------------------------------------------------------
def test_a14_hidden_source_reads_as_unavailable_without_rewriting_period(tmp_path):
    store, service, model = make_service(tmp_path)
    window = reflection_window("DAILY", NOW, OFFSET)
    event = seed_event(store, window.start + timedelta(hours=1))
    try:
        assert service.run_period("c00", "DAILY", now=NOW)["status"] == "COMMITTED"
        before = get_period(store)["output_json"]
        store.update_event_metadata(event.id, {"hidden": True})
        listed = ReflectionReader(store).list("c00", kind="DAILY", limit=10)
        assert listed["periods"][0]["evidence_refs"][0]["status"] == "UNAVAILABLE"
        assert get_period(store)["output_json"] == before
    finally:
        store.close()


# A15 ------------------------------------------------------------------------
def test_a15_every_run_has_reason_and_committed_has_provenance(tmp_path):
    store, service, model = make_service(tmp_path)
    idle = service.run_period("c00", "DAILY", now=NOW)
    assert idle["status"] == "SKIPPED_IDLE" and idle["reason"]
    next_now = NOW + timedelta(days=1)
    window = reflection_window("DAILY", next_now, OFFSET)
    seed_event(store, window.start + timedelta(hours=1), content="后来的证据")
    committed = service.run_period("c00", "DAILY", now=next_now)
    assert committed["status"] == "COMMITTED" and committed["reason"]
    assert committed["policy_version"] == POLICY_VERSION
    assert committed["window_start"] and committed["window_end"]
    assert committed["evidence"]["facts"]
    try:
        listed = ReflectionReader(store).list("c00", kind="DAILY", limit=10)
        assert listed["periods"][0]["reason"]
    finally:
        store.close()


# A16 ------------------------------------------------------------------------
def test_a16_evidence_is_truncated_in_a_fixed_order_and_counted(tmp_path):
    store, service, model = make_service(tmp_path)
    model.refs = []
    window = reflection_window("DAILY", NOW, OFFSET)
    for index in range(205):
        seed_event(store, window.start + timedelta(seconds=index), content="x")
    try:
        result = service.run_period("c00", "DAILY", now=NOW)
        assert result["status"] == "COMMITTED"
        facts = result["evidence"]["facts"]
        # The daily input-character budget (6000) is reached before the 200-fact
        # ceiling, so the newest facts survive and every dropped one is counted.
        assert 0 < len(facts) <= 200
        assert result["evidence"]["chars"] <= 6000
        assert result["evidence"]["governance"]["facts_truncated"] == 205 - len(facts)
        kept = [item["id"] for item in facts]
        assert kept == sorted(kept, reverse=True)
        assert 205 in kept and 1 not in kept
        assert len(model.calls) == 1
    finally:
        store.close()


def test_single_oversize_fact_is_trimmed_not_treated_as_idle(tmp_path):
    store, service, model = make_service(tmp_path)
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store, window.start + timedelta(hours=1), content="x" * 20000)
    try:
        result = service.run_period("c00", "DAILY", now=NOW)
        assert result["status"] == "COMMITTED"
        assert len(result["evidence"]["facts"]) == 1
        assert result["evidence"]["chars"] <= 6000
        assert len(model.calls) == 1
    finally:
        store.close()


# A17 ------------------------------------------------------------------------
def test_a17_oversize_output_fails_without_partial_reflection(tmp_path):
    store, service, model = make_service(tmp_path, model=FakeReflectionModel(mode="oversize"))
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store, window.start + timedelta(hours=1))
    try:
        result = service.run_period("c00", "DAILY", now=NOW)
        assert result["status"] == "FAILED" and result["output"] is None
        row = store.conn.execute("SELECT status FROM model_invocation_receipts").fetchone()
        assert row["status"] == "FAILED"
    finally:
        store.close()


# A18 ------------------------------------------------------------------------
def test_a18_llm_usage_scope_is_reflection_not_other(tmp_path):
    store, service, model = make_service(tmp_path)
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store, window.start + timedelta(hours=1))
    try:
        assert service.run_period("c00", "DAILY", now=NOW)["status"] == "COMMITTED"
        scope = model.scopes[0]
        assert scope.feature == "REFLECTION"
        assert scope.purpose == "REFLECTION_DAILY"
        assert scope.character_id == "c00"
        inferred = infer_usage_context("reflection:WEEKLY:c00:2026-10-18T16:00:00+00:00")
        assert (inferred.feature, inferred.purpose) == ("REFLECTION", "REFLECTION_WEEKLY")
        from pathlib import Path
        dev_js = Path("src/character_memory/web/dev.js").read_text(encoding="utf-8")
        assert "REFLECTION_DAILY" in dev_js and "REFLECTION_MONTHLY" in dev_js
    finally:
        store.close()


# A19 ------------------------------------------------------------------------
def test_a19_windows_do_not_overlap_and_future_window_is_refused(tmp_path, monkeypatch):
    daily = reflection_window("DAILY", NOW, OFFSET)
    assert daily.end - daily.start == timedelta(days=1)
    assert daily.start.astimezone(timezone(timedelta(minutes=OFFSET))).hour == 0
    assert daily.end.astimezone(timezone(timedelta(minutes=OFFSET))).hour == 0

    weekly = reflection_window("WEEKLY", NOW, OFFSET)
    assert weekly.end - weekly.start == timedelta(days=7)
    previous_week = reflection_window("WEEKLY", weekly.start, OFFSET)
    assert previous_week.end == weekly.start
    assert previous_week.start < previous_week.end <= weekly.start

    monthly = reflection_window("MONTHLY", NOW, OFFSET)
    local_start = monthly.start.astimezone(timezone(timedelta(minutes=OFFSET)))
    local_end = monthly.end.astimezone(timezone(timedelta(minutes=OFFSET)))
    assert local_start.day == 1 and local_end.day == 1
    assert (local_end.year, local_end.month) == (2026, 10)
    assert (local_start.year, local_start.month) == (2026, 9)

    store, service, model = make_service(tmp_path)
    try:
        import character_memory.application.reflection_service as module
        monkeypatch.setattr(
            module, "reflection_window",
            lambda kind, now, offset: module.ReflectionWindow(
                kind, now + timedelta(days=1), now + timedelta(days=2), offset,
            ),
        )
        result = service.run_period("c00", "DAILY", now=NOW)
        assert result["reason"] == "FUTURE_WINDOW"
        assert period_count(store) == 0
        assert model.calls == []
    finally:
        store.close()


# A20 ------------------------------------------------------------------------
def test_a20_scheduler_start_is_idempotent_and_stop_joins(tmp_path):
    ticks = []

    def runner():
        ticks.append(1)

    scheduler = ReflectionScheduler(runner, poll_seconds=30, enabled=True)
    scheduler.start()
    try:
        first_thread = scheduler._thread
        scheduler.start()
        assert scheduler._thread is first_thread
        assert scheduler.running
    finally:
        scheduler.stop()
    assert not scheduler.running
    assert ticks


# A21 ------------------------------------------------------------------------
def test_a21_reflection_does_not_touch_world_space_or_group_clocks(tmp_path):
    from character_memory.group_store import GroupRepository
    from character_memory.space_store import SpaceRepository
    from character_memory.world_activity import WorldPulseRepository

    store, service, model = make_service(tmp_path)
    GroupRepository(store)
    SpaceRepository(store)
    WorldPulseRepository(store)
    clocks = ("world_browse_decisions", "space_daily_runs", "group_autonomy_state",
              "group_autonomy_runs", "world_states")
    before = {table: _table_count(store, table) for table in clocks}
    window = reflection_window("DAILY", NOW, OFFSET)
    seed_event(store, window.start + timedelta(hours=1))
    try:
        assert service.run_period("c00", "DAILY", now=NOW)["status"] == "COMMITTED"
        assert {table: _table_count(store, table) for table in clocks} == before
    finally:
        store.close()


# Real HTTP + real temporary SQLite: daily/weekly/monthly through the read API.
def test_real_http_lists_one_daily_weekly_monthly_period(tmp_path):
    store, service, model = make_service(tmp_path)
    for kind in ("DAILY", "WEEKLY", "MONTHLY"):
        window = reflection_window(kind, NOW, OFFSET)
        seed_event(store, window.start + timedelta(hours=1), content=f"{kind} 的真实经历",
                   event_type=EventType.WORLD_OBSERVATION)
    for kind in ("DAILY", "WEEKLY", "MONTHLY"):
        assert service.run_period("c00", kind, now=NOW)["status"] == "COMMITTED"
    app = FastAPI()
    access = SimpleNamespace(read_store=store, ensure_character=lambda character_id: None)
    attach_reflection_routes(app, access)
    try:
        with TestClient(app) as client:
            response = client.get("/v1/characters/c00/reflections?limit=10")
            assert response.status_code == 200
            body = response.json()
            assert body["read_only"] is True
            assert {item["kind"] for item in body["periods"]} == {"DAILY", "WEEKLY", "MONTHLY"}
            assert all(item["status"] == "COMMITTED" for item in body["periods"])
            assert all(item["evidence_refs"] for item in body["periods"])
            filtered = client.get("/v1/characters/c00/reflections?kind=WEEKLY&limit=1").json()
            assert [item["kind"] for item in filtered["periods"]] == ["WEEKLY"]
            assert client.get("/v1/characters/c00/reflections?kind=HOURLY").status_code == 422
    finally:
        store.close()
