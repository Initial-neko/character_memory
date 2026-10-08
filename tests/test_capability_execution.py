from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import replace
from threading import Event as Signal

import pytest

from character_memory.domain.models import WorldObservation
from character_memory.storage.sqlite import SQLiteStore
from character_memory.world_activity import PersonalBrowsePlan
from character_memory.runtime.capability_execution import CapabilityExecutor, CapabilityRequest, adapt_browse_decision

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


class Observer:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
    def observe(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if self.error:
            raise self.error
        return {"search_results": 1, "errors": [], "observations": [WorldObservation(
            title="Memory", url="https://example.org/memory", source_domain="example.org", content="real page") ]}


def request():
    return adapt_browse_decision(PersonalBrowsePlan(browse=True, query="agent memory"),
                                character_id="a", opportunity_id="world-run:17", source_event_id=12)


def test_no_action_has_no_request_or_capability_call():
    assert adapt_browse_decision(PersonalBrowsePlan(browse=False), character_id="a",
                                opportunity_id="world-run:17", source_event_id=None) is None
    result = request()
    assert result.character_id == "a" and result.opportunity_id == "world-run:17"
    assert result.source_event_id == 12 and result.request_id.endswith("WEB_SEARCH:1")


def test_success_is_durable_and_duplicate_does_not_execute_provider_after_restart(tmp_path):
    path = tmp_path / "local.sqlite"
    store = SQLiteStore(path)
    observer = Observer()
    result = CapabilityExecutor(store, observer).execute(request(), now=NOW)
    assert result.status == "SUCCESS" and result.data["observations"][0]["content"] == "real page"
    assert len(observer.calls) == 1
    store.close()
    store = SQLiteStore(path)
    duplicate = CapabilityExecutor(store, observer).execute(request(), now=NOW)
    assert duplicate == result and len(observer.calls) == 1
    store.close()


@pytest.mark.parametrize("changes,reason", [({"channel": "DIRECT"}, "CHANNEL_DENIED"),
    ({"capability": "SYSTEM_COMMAND"}, "CAPABILITY_DENIED"),
    ({"arguments": {"query": ""}}, "INVALID_ARGUMENTS"),
    ({"constraints": {"max_pages": 5, "max_chars_per_page": 6000, "max_calls": 1}}, "INVALID_CONSTRAINTS"),
    ({"constraints": {"max_pages": 2, "max_chars_per_page": 6000, "max_calls": 0}}, "BUDGET_EXHAUSTED")])
def test_denied_requests_never_touch_provider(tmp_path, changes, reason):
    store = SQLiteStore(tmp_path / "local.sqlite")
    observer = Observer()
    result = CapabilityExecutor(store, observer).execute(replace(request(), **changes), now=NOW)
    assert result.status == "DENIED" and result.reason == reason and observer.calls == []
    store.close()


def test_same_id_different_arguments_is_denied_without_leaking_old_result(tmp_path):
    store = SQLiteStore(tmp_path / "local.sqlite")
    observer = Observer()
    executor = CapabilityExecutor(store, observer)
    executor.execute(request(), now=NOW)
    result = executor.execute(replace(request(), arguments={"query": "other topic"}), now=NOW)
    assert result.status == "DENIED" and result.reason == "REQUEST_ID_CONFLICT"
    assert result.data == {} and len(observer.calls) == 1
    store.close()


@pytest.mark.parametrize("error,status", [(RuntimeError("offline"), "FAILED"), (TimeoutError("timeout"), "TIMEOUT")])
def test_failed_attempt_consumes_opportunity_budget_and_does_not_replay(tmp_path, error, status):
    store = SQLiteStore(tmp_path / "local.sqlite")
    observer = Observer(error)
    executor = CapabilityExecutor(store, observer)
    first = executor.execute(request(), now=NOW)
    assert first.status == status
    assert executor.execute(request(), now=NOW) == first and len(observer.calls) == 1
    other = replace(request(), request_id="world-run:17:WEB_SEARCH:2")
    denied = executor.execute(other, now=NOW)
    assert denied.status == "DENIED" and denied.reason == "BUDGET_EXHAUSTED"
    assert len(observer.calls) == 1
    store.close()


def test_started_unknown_after_restart_is_skipped_without_replaying_provider(tmp_path):
    import json
    from dataclasses import asdict
    path = tmp_path / "local.sqlite"
    store = SQLiteStore(path)
    observer = Observer()
    executor = CapabilityExecutor(store, observer)
    store.conn.execute("INSERT INTO capability_executions(request_id,character_id,opportunity_id,request_json,status,started_at) VALUES(?,?,?,?,?,?)",
                       (request().request_id, "a", "world-run:17", json.dumps(asdict(request()), sort_keys=True), "STARTED", NOW.isoformat()))
    store.conn.commit()
    store.close()
    store = SQLiteStore(path)
    result = CapabilityExecutor(store, observer).execute(request(), now=NOW)
    assert result.status == "SKIPPED" and result.reason == "IN_PROGRESS_OR_INTERRUPTED" and observer.calls == []
    store.close()


def test_two_connections_execute_same_request_only_once(tmp_path):
    path = tmp_path / "local.sqlite"
    stores = [SQLiteStore(path), SQLiteStore(path)]
    observer = Observer()
    executors = [CapabilityExecutor(store, observer) for store in stores]
    entered = Signal()
    release = Signal()
    original = observer.observe
    def blocked(query, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(query, **kwargs)
    observer.observe = blocked
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(executors[0].execute, request(), now=NOW)
            assert entered.wait(2)
            second = pool.submit(executors[1].execute, request(), now=NOW).result()
            release.set()
            completed = first.result()
        assert second.status == "SKIPPED" and completed.status == "SUCCESS" and len(observer.calls) == 1
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("changes", [{"arguments": None}, {"constraints": None},
                                      {"source_event_id": -1}, {"character_id": ""}])
def test_malformed_request_is_denied_without_external_execution(tmp_path, changes):
    store = SQLiteStore(tmp_path / "local.sqlite")
    observer = Observer()
    result = CapabilityExecutor(store, observer).execute(replace(request(), **changes), now=NOW)
    assert result.status == "DENIED" and observer.calls == []
    store.close()


def test_existing_world_observer_is_the_actual_adapter_and_bounds_content(tmp_path):
    from test_world_observation import FakeSearchProvider, AllReachableFetcher
    from character_memory.world_observation import WorldObservationService
    store = SQLiteStore(tmp_path / "local.sqlite")
    fetcher = AllReachableFetcher()
    observer = WorldObservationService(FakeSearchProvider(), fetcher)
    result = CapabilityExecutor(store, observer).execute(request(), now=NOW)
    assert result.status == "SUCCESS" and result.data["observations"]
    assert len(result.data["observations"]) <= 2
    assert all(len(page["content"]) <= 6000 for page in result.data["observations"])
    store.close()


def test_receipt_write_failure_leaves_unknown_and_never_replays_provider(tmp_path):
    import sqlite3
    store = SQLiteStore(tmp_path / "local.sqlite")
    observer = Observer()
    executor = CapabilityExecutor(store, observer)
    store.conn.execute("CREATE TRIGGER reject_receipt BEFORE UPDATE ON capability_executions BEGIN SELECT RAISE(ABORT, 'receipt failed'); END")
    store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="receipt failed"):
        executor.execute(request(), now=NOW)
    assert len(observer.calls) == 1
    store.conn.execute("DROP TRIGGER reject_receipt")
    store.conn.commit()
    result = executor.execute(request(), now=NOW)
    assert result.status == "SKIPPED" and result.reason == "IN_PROGRESS_OR_INTERRUPTED"
    assert len(observer.calls) == 1
    store.close()
