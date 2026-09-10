from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest

from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    Event,
    EventType,
    PersonReaction,
)
from character_memory.eval.runner import EvalRunner
from character_memory.life.runner import DayRunner
from character_memory.llm.client import ModelCallResult, ModelCallTrace, PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.stickers import import_sticker_bundle, load_global_sticker_catalog, load_sticker_catalog
from character_memory.storage.sqlite import SQLiteStore


class PerCallTraceModel(PersonModel):
    """Returns exact invocation data while deliberately poisoning shared last_* fields."""

    def react(self, context: str) -> PersonReaction:
        return PersonReaction(actions=[])

    def react_call_for_session(self, context: str, session_id: str) -> ModelCallResult:
        trace = ModelCallTrace(
            request_messages=[{"role": "user", "content": f"request:{session_id}"}],
            response_text=f"response:{session_id}",
            attempt=1,
            model=f"model:{session_id}",
        )
        # These are intentionally wrong. Runtime persistence must never use them.
        self.last_request_messages = [{"role": "user", "content": "WRONG-SHARED-REQUEST"}]
        self.last_response_text = "WRONG-SHARED-RESPONSE"
        self.last_attempt = 99
        self.last_model = "WRONG-SHARED-MODEL"
        return ModelCallResult(value=PersonReaction(actions=[]), trace=trace)

    def plan_day(self, context: str) -> DailyLifePlan:
        return DailyLifePlan()

    def write_diary(self, context: str) -> DiaryResult:
        return DiaryResult(diary="", mental_state_update="")


def test_runtime_trace_uses_exact_model_call_snapshot_not_shared_last_fields(tmp_path: Path):
    store = SQLiteStore(tmp_path / "trace.db")
    embeddings = DeterministicEmbedding()
    model = PerCallTraceModel()
    runtimes = {
        character_id: PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, character_id)
        for character_id in ("a", "b")
    }
    results = {}

    def run(character_id: str):
        results[character_id] = runtimes[character_id].handle(
            Event(
                character_id=character_id,
                event_type=EventType.USER_MESSAGE,
                event_time=datetime(2026, 9, 11, 8, tzinfo=timezone.utc),
                content=f"hello-{character_id}",
                metadata={"conversation_id": f"conversation-{character_id}"},
            )
        )

    threads = [threading.Thread(target=run, args=(character_id,)) for character_id in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    for character_id in ("a", "b"):
        trace = store.get_runtime_trace(results[character_id].event.id)
        assert trace["model_messages"] == [{"role": "user", "content": f"request:conversation-{character_id}"}]
        assert trace["raw_model_response"] == f"response:conversation-{character_id}"
        assert trace["model_attempt"] == 1
        assert trace["model_used"] == f"model:conversation-{character_id}"
        assert "WRONG-SHARED" not in json.dumps(trace)
    store.close()


class BlockingGroupModel(PersonModel):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.contexts: list[str] = []
        self._calls = 0
        self._guard = threading.Lock()

    def react(self, context: str) -> PersonReaction:
        return self.react_for_session(context, "default")

    def react_for_session(self, context: str, session_id: str) -> PersonReaction:
        with self._guard:
            self._calls += 1
            call_number = self._calls
            self.contexts.append(context)
        if call_number == 1:
            self.entered.set()
            assert self.release.wait(timeout=2)
        return PersonReaction(actions=[])

    def plan_day(self, context: str) -> DailyLifePlan:
        return DailyLifePlan()

    def write_diary(self, context: str) -> DiaryResult:
        return DiaryResult(diary="", mental_state_update="")


def test_two_group_services_sharing_app_lock_cannot_interleave_user_turns(tmp_path: Path):
    store = SQLiteStore(tmp_path / "group.db")
    embeddings = DeterministicEmbedding()
    model = BlockingGroupModel()
    runtimes = {
        character_id: PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, character_id)
        for character_id in ("rin", "momo")
    }
    lock = threading.RLock()
    clock = FixedClock(datetime(2026, 9, 11, 8, tzinfo=timezone.utc))
    first_service = GroupConversationService(store, runtimes, clock, turn_lock=lock)
    group = first_service.create_group("并发群", ["rin", "momo"])
    second_service = GroupConversationService(store, runtimes, clock, turn_lock=lock)
    errors: list[BaseException] = []

    def send(service, text):
        try:
            service.send(group.id, text)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    first = threading.Thread(target=send, args=(first_service, "第一轮"))
    first.start()
    assert model.entered.wait(timeout=1)

    second = threading.Thread(target=send, args=(second_service, "第二轮"))
    second.start()
    time.sleep(0.08)

    # The second request has not even persisted its USER fact yet.
    while_first_blocked = first_service.repo.list_events(group.id)
    assert [item.content for item in while_first_blocked if item.actor_type == "USER"] == ["第一轮"]

    model.release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert "第二轮" not in model.contexts[0]
    final_user_messages = [item.content for item in first_service.repo.list_events(group.id) if item.actor_type == "USER"]
    assert final_user_messages == ["第一轮", "第二轮"]
    store.close()


def test_event_and_intent_time_queries_use_absolute_time_not_iso_lexical_order(tmp_path: Path):
    store = SQLiteStore(tmp_path / "time.db")
    past_with_later_local_date = datetime.fromisoformat("2026-09-11T00:30:00+08:00")  # 2026-09-10 16:30Z
    future_with_earlier_local_date = datetime.fromisoformat("2026-09-10T23:30:00-07:00")  # 2026-09-11 06:30Z
    store.append_event(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=past_with_later_local_date, content="past"))
    store.append_event(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=future_with_earlier_local_date, content="future"))

    at_1700z = datetime.fromisoformat("2026-09-10T17:00:00+00:00")
    assert [event.content for event in store.list_events("rin", before=at_1700z)] == ["past"]
    at_0600z = datetime.fromisoformat("2026-09-11T06:00:00+00:00")
    assert [event.content for event in store.list_events("rin", before=at_0600z)] == ["past"]

    now = datetime.fromisoformat("2026-09-11T00:00:00+00:00")
    due_id = store.add_intent(
        "rin",
        "already due",
        "MESSAGE",
        now - timedelta(hours=2),
        datetime.fromisoformat("2026-09-11T07:30:00+08:00"),  # 23:30Z previous day
        now + timedelta(hours=3),
    )
    future_id = store.add_intent(
        "rin",
        "not yet",
        "MESSAGE",
        now - timedelta(hours=2),
        datetime.fromisoformat("2026-09-10T18:30:00-07:00"),  # 01:30Z next
        now + timedelta(hours=4),
    )
    assert [row["id"] for row in store.due_intents("rin", now)] == [due_id]
    assert future_id not in [row["id"] for row in store.due_intents("rin", now)]
    store.close()


def test_legacy_iso_rows_are_backfilled_with_epoch_keys(tmp_path: Path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, character_id TEXT NOT NULL, event_type TEXT NOT NULL, event_time TEXT NOT NULL, content TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')"
    )
    conn.execute(
        "INSERT INTO events(character_id,event_type,event_time,content,metadata_json) VALUES(?,?,?,?,?)",
        ("rin", "USER_MESSAGE", "2026-09-11T00:30:00+08:00", "legacy", "{}"),
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(events)").fetchall()}
    assert "event_time_epoch" in columns
    row = store.conn.execute("SELECT event_time_epoch FROM events WHERE content='legacy'").fetchone()
    assert row["event_time_epoch"] is not None
    cutoff = datetime.fromisoformat("2026-09-10T17:00:00+00:00")
    assert [event.content for event in store.list_events("rin", before=cutoff)] == ["legacy"]
    store.close()


class CaptureContextModel(PersonModel):
    def __init__(self):
        self.contexts: list[str] = []

    def react(self, context: str) -> PersonReaction:
        self.contexts.append(context)
        return PersonReaction(actions=[])

    def plan_day(self, context: str) -> DailyLifePlan:
        return DailyLifePlan()

    def write_diary(self, context: str) -> DiaryResult:
        return DiaryResult(diary="", mental_state_update="")


def test_future_mental_state_does_not_leak_into_past_runtime_context(tmp_path: Path):
    store = SQLiteStore(tmp_path / "state.db")
    past = datetime(2026, 9, 10, 10, tzinfo=timezone.utc)
    middle = datetime(2026, 9, 11, 10, tzinfo=timezone.utc)
    future = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    store.set_mental_state("rin", "past-state", past, 1)
    store.set_mental_state("rin", "future-state", future, 2)
    store.set_world_time("rin", future)

    embeddings = DeterministicEmbedding()
    model = CaptureContextModel()
    runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "persona")
    result = runtime.handle(
        Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=middle, content="现实时间聊天")
    )

    assert result.mental_state_before == "past-state"
    assert "past-state" in result.context
    assert "future-state" not in result.context
    assert store.get_mental_state("rin") == "future-state"
    assert store.get_mental_state("rin", at=middle) == "past-state"

    # Ordinary chat must not rewind the persistent virtual-time cursor either.
    store.set_world_time("rin", middle)
    assert store.get_world_time("rin") == future
    store.close()


def _sticker_zip(rows: list[dict], files: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("all_tags.json", json.dumps(rows, ensure_ascii=False))
        for name, payload in files.items():
            archive.writestr(name, payload)
    return output.getvalue()


def _sticker_row(sticker_id: str, filename: str, label: str) -> dict:
    return {
        "id": sticker_id,
        "filename": filename,
        "set_id": "review",
        "display_name": "审查包",
        "tag_zh": label,
        "aliases": [label],
    }


def test_failed_sticker_import_keeps_previous_manifest_and_asset_unchanged(tmp_path: Path):
    persona = tmp_path / "personas" / "rin" / "persona.yaml"
    persona.parent.mkdir(parents=True)
    persona.write_text("id: rin\nname: Rin\n", encoding="utf-8")

    initial = _sticker_zip([_sticker_row("same", "same.png", "旧表情")], {"same.png": b"old-bytes"})
    import_sticker_bundle(persona, initial)
    manifest = persona.parent / "stickers" / "manifest.yaml"
    before_manifest = manifest.read_bytes()
    before_catalog = load_sticker_catalog(persona)
    before_asset_path = before_catalog.asset_path("same")
    assert before_asset_path is not None
    assert before_asset_path.read_bytes() == b"old-bytes"

    broken = _sticker_zip(
        [
            _sticker_row("same", "same.png", "新表情"),
            _sticker_row("missing", "missing.png", "缺失"),
        ],
        {"same.png": b"new-bytes"},
    )
    with pytest.raises(ValueError, match="cannot uniquely locate sticker asset"):
        import_sticker_bundle(persona, broken)

    assert manifest.read_bytes() == before_manifest
    after_catalog = load_sticker_catalog(persona)
    assert after_catalog.asset_path("same") == before_asset_path
    assert after_catalog.asset_path("same").read_bytes() == b"old-bytes"


def test_concurrent_sticker_imports_merge_instead_of_losing_manifest_updates(tmp_path: Path):
    persona = tmp_path / "personas" / "rin" / "persona.yaml"
    persona.parent.mkdir(parents=True)
    persona.write_text("id: rin\nname: Rin\n", encoding="utf-8")
    target = tmp_path / "global-stickers"
    archives = [
        _sticker_zip([_sticker_row("a", "a.png", "A")], {"a.png": b"A"}),
        _sticker_zip([_sticker_row("b", "b.png", "B")], {"b.png": b"B"}),
    ]
    errors: list[BaseException] = []

    def run(payload):
        try:
            import_sticker_bundle(persona, payload, target_dir=target)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(payload,)) for payload in archives]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert errors == []

    catalog = load_global_sticker_catalog(target)
    assert catalog.get("a") is not None
    assert catalog.get("b") is not None
    assert catalog.asset_path("a").read_bytes() == b"A"
    assert catalog.asset_path("b").read_bytes() == b"B"


class EmptyLife:
    def simulate_day(self, character_id, day):
        return []

    def end_day(self, character_id, day):
        return SimpleNamespace(diary="")


class EmptyTicker:
    def tick(self, character_id, at):
        return []


def test_consecutive_simulation_runs_do_not_skip_dates(tmp_path: Path):
    store = SQLiteStore(tmp_path / "days.db")
    store.set_world_time("rin", datetime(2026, 9, 11, 10, tzinfo=timezone.utc))
    runner = DayRunner(store, EmptyLife(), EmptyTicker())

    result = runner.simulate("rin", 3, tick_hours=())

    assert [item["date"] for item in result] == ["2026-09-12", "2026-09-13", "2026-09-14"]
    assert store.get_world_time("rin") == datetime(2026, 9, 15, 0, tzinfo=timezone.utc)
    store.close()


class FakeEvalStore:
    def get_runtime_trace(self, source_event_id):
        return {}


class ResourceEvalRuntime:
    def __init__(self):
        self.store = FakeEvalStore()
        self._next_id = 0

    def handle(self, event):
        self._next_id += 1
        if "image" in event.content:
            action = ActionDecision(type=ActionType.IMAGE, image_id="image-1")
        else:
            action = ActionDecision(type=ActionType.STICKER, sticker_id="sticker-1")
        return SimpleNamespace(
            event=event.model_copy(update={"id": self._next_id}),
            reaction=PersonReaction(actions=[action]),
            recalled_memories=[],
            created_memory_ids=[],
            context="",
        )


def test_eval_does_not_treat_sticker_or_image_as_silence(tmp_path: Path):
    cases = tmp_path / "resource-eval.jsonl"
    cases.write_text(
        "\n".join(
            [
                json.dumps({"id":"sticker","event_time":"2026-09-11T08:00:00+00:00","content":"sticker","expect_silence":True}),
                json.dumps({"id":"image","event_time":"2026-09-11T08:01:00+00:00","content":"image","expect_silence":True}),
            ]
        ),
        encoding="utf-8",
    )

    results = EvalRunner(ResourceEvalRuntime()).run_jsonl(cases)

    assert [result["silent"] for result in results] == [False, False]
    assert [result["pass"] for result in results] == [False, False]
