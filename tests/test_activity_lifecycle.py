"""Activity lifecycle: state machine, versions, idempotency and identity boundary."""
import sqlite3
import threading

import pytest
from pydantic import ValidationError

from character_memory.domain.activity import (
    ActivityAction,
    ActivityCreate,
    activity_experience_scope,
    transition_target,
)
from character_memory.domain.experience_scope import is_real_experience
from character_memory.storage.activities import (
    ActivityConflict,
    ActivityMissing,
    ActivityRepository,
)
from character_memory.storage.sqlite import SQLiteStore


def request(**changes):
    data = {
        "request_id": "req-create-0001",
        "title": "周末一起整理房间",
        "participants": [
            {"actor_type": "CHARACTER", "character_id": "rin"},
            {"actor_type": "SIMULATED_CHARACTER", "local_id": "neighbor", "display_name": "隔壁的邻居"},
        ],
    }
    data.update(changes)
    return ActivityCreate(**data)


def action(version, request_id="req-action-0001"):
    return ActivityAction(request_id=request_id, expected_version=version)


@pytest.fixture
def state(tmp_path):
    store = SQLiteStore(tmp_path / "activity.sqlite")
    repo = ActivityRepository(store)
    yield store, repo
    store.close()


def test_created_activity_is_not_implicitly_active(state):
    store, repo = state
    created = repo.create(request())
    activity = created["activity"]
    assert activity["status"] == "CREATED"
    assert activity["version"] == 1
    assert created["replayed"] is False
    assert created["receipt"] == {"request_id": "req-create-0001", "operation": "CREATE",
                                  "version_applied": 1}
    assert [item["actor_type"] for item in activity["participants"]] == [
        "CHARACTER", "SIMULATED_CHARACTER"]
    assert activity["participants"][0]["character_id"] == "rin"
    assert activity["participants"][1]["local_id"] == "neighbor"
    assert repo.get(activity["id"])["status"] == "CREATED"


def test_resume_pause_end_walk_and_terminal_end(state):
    store, repo = state
    gid = repo.create(request())["activity"]["id"]
    resumed = repo.act(gid, "RESUME", action(1))["activity"]
    assert (resumed["status"], resumed["version"]) == ("ACTIVE", 2)
    paused = repo.act(gid, "PAUSE", action(2, "req-action-0002"))["activity"]
    assert (paused["status"], paused["version"]) == ("PAUSED", 3)
    again = repo.act(gid, "RESUME", action(3, "req-action-0003"))["activity"]
    assert (again["status"], again["version"]) == ("ACTIVE", 4)
    ended = repo.act(gid, "END", action(4, "req-action-0004"))["activity"]
    assert (ended["status"], ended["version"]) == ("ENDED", 5)
    # ENDED is terminal: every further operation is a state conflict, not a
    # silent no-op that would let a caller believe it re-opened the activity.
    for index, operation in enumerate(("RESUME", "PAUSE", "END")):
        with pytest.raises(ActivityConflict):
            repo.act(gid, operation, action(5, f"req-action-000{5 + index}"))
    assert repo.get(gid)["version"] == 5


def test_stale_version_conflicts_without_overwrite(state):
    store, repo = state
    gid = repo.create(request())["activity"]["id"]
    repo.act(gid, "RESUME", action(1))
    with pytest.raises(ActivityConflict) as stale:
        repo.act(gid, "PAUSE", action(1, "req-action-stale"))
    assert "ACTIVITY_VERSION_CONFLICT" in str(stale.value)
    assert repo.get(gid)["status"] == "ACTIVE" and repo.get(gid)["version"] == 2


def test_repeated_action_is_idempotent_and_never_reapplies(state):
    store, repo = state
    created = repo.create(request())
    gid = created["activity"]["id"]
    first = repo.act(gid, "RESUME", action(1))
    replay = repo.act(gid, "RESUME", action(1))
    assert replay["replayed"] is True
    assert replay["activity"] == first["activity"]
    assert replay["receipt"] == first["receipt"]
    assert repo.get(gid)["version"] == 2
    # The same request_id cannot be rebound to a different operation.
    with pytest.raises(ActivityConflict):
        repo.act(gid, "PAUSE", action(2))
    create_replay = repo.create(request())
    assert create_replay["replayed"] is True and create_replay["activity"]["version"] == 2


def test_request_identity_cannot_cross_activities(state):
    store, repo = state
    first = repo.create(request())["activity"]["id"]
    second = repo.create(request(request_id="req-create-0002", title="另一件事"))["activity"]["id"]
    repo.act(first, "RESUME", action(1, "req-cross-00001"))
    with pytest.raises(ActivityConflict):
        repo.act(second, "RESUME", action(1, "req-cross-00001"))
    with pytest.raises(ActivityMissing):
        repo.get("does-not-exist")


def test_simulated_participant_never_becomes_a_person(state):
    store, repo = state
    created = repo.create(request())
    simulated = created["activity"]["participants"][1]
    assert simulated["actor_type"] == "SIMULATED_CHARACTER"
    assert "character_id" not in simulated
    # The only durable trace of the simulated identity is the activity-local row.
    rows = store.conn.execute(
        "SELECT participant_key,character_id FROM activity_participants WHERE actor_type='SIMULATED_CHARACTER'"
    ).fetchall()
    assert [(row["participant_key"], row["character_id"]) for row in rows] == [("sim:neighbor", None)]
    # Nothing in this repository writes a Persona or a registry row for a
    # simulated participant; the table above is the only durable trace.
    assert store.conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='personas'"
    ).fetchone()[0] == 0
    with pytest.raises(ValidationError):
        request(participants=[{"actor_type": "CHARACTER", "character_id": "sim:neighbor"}])


def test_participant_contract_rejects_ambiguous_identity():
    with pytest.raises(ValidationError):
        request(participants=[
            {"actor_type": "CHARACTER", "character_id": "rin"},
            {"actor_type": "CHARACTER", "character_id": "rin"},
        ])
    with pytest.raises(ValidationError):
        request(participants=[
            {"actor_type": "CHARACTER", "character_id": "rin"},
            {"actor_type": "SIMULATED_CHARACTER", "local_id": "rin", "display_name": "冒名"},
        ])
    with pytest.raises(ValidationError):
        request(participants=[])
    with pytest.raises(ValidationError):
        request(title="   ")


def test_activity_scope_is_simulated_and_cannot_enter_real_runtime(state):
    store, repo = state
    activity = repo.create(request())["activity"]
    scope = activity_experience_scope(activity["id"])
    assert scope == {"schema_version": 1, "reality": "SIMULATED", "activity_id": activity["id"]}
    assert is_real_experience({"experience_scope": scope}) is False
    with pytest.raises(ValueError):
        activity_experience_scope("  ")


def test_state_machine_is_pure_and_total():
    assert transition_target("CREATED", "RESUME") == "ACTIVE"
    assert transition_target("CREATED", "END") == "ENDED"
    assert transition_target("ACTIVE", "PAUSE") == "PAUSED"
    assert transition_target("PAUSED", "RESUME") == "ACTIVE"
    for status, operation in [
        ("CREATED", "PAUSE"), ("ACTIVE", "RESUME"), ("PAUSED", "PAUSE"),
        ("ENDED", "RESUME"), ("ENDED", "PAUSE"), ("ENDED", "END"),
    ]:
        with pytest.raises(ValueError):
            transition_target(status, operation)


def test_list_is_bounded_and_filters_status(state):
    store, repo = state
    first = repo.create(request())["activity"]["id"]
    repo.create(request(request_id="req-create-0002", title="第二件"))
    repo.act(first, "RESUME", action(1))
    assert [item["id"] for item in repo.list(limit=1)] == [first]
    assert [item["status"] for item in repo.list(status="CREATED")] == ["CREATED"]
    assert [item["status"] for item in repo.list(status="ACTIVE")] == ["ACTIVE"]
    with pytest.raises(ValueError):
        repo.list(limit=0)
    with pytest.raises(ValueError):
        repo.list(status="UNKNOWN")


def test_state_persists_across_reopen(tmp_path):
    path = tmp_path / "restart.sqlite"
    store = SQLiteStore(path)
    repo = ActivityRepository(store)
    gid = repo.create(request())["activity"]["id"]
    repo.act(gid, "RESUME", action(1))
    store.close()

    reopened = SQLiteStore(path)
    try:
        again = ActivityRepository(reopened).get(gid)
        assert (again["status"], again["version"]) == ("ACTIVE", 2)
        assert again["participants"][1]["display_name"] == "隔壁的邻居"
        resumed = ActivityRepository(reopened).act(gid, "PAUSE", action(2, "req-after-restart"))
        assert resumed["activity"]["status"] == "PAUSED"
    finally:
        reopened.close()


def test_two_connections_same_version_only_one_wins(tmp_path):
    path = tmp_path / "race.sqlite"
    stores = [SQLiteStore(path), SQLiteStore(path)]
    repos = [ActivityRepository(item) for item in stores]
    try:
        gid = repos[0].create(request())["activity"]["id"]
        gate = threading.Barrier(2)
        outcomes = []

        def attempt(index):
            gate.wait()
            try:
                outcomes.append(repos[index].act(gid, "RESUME", action(1, f"req-race-0000{index}")))
            except ActivityConflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(str(item) if item == "conflict" else "ok" for item in outcomes) == ["conflict", "ok"]
        assert repos[0].get(gid)["version"] == 2
    finally:
        for item in stores:
            item.close()


def test_receipts_are_durable_across_restart(tmp_path):
    path = tmp_path / "receipts.sqlite"
    store = SQLiteStore(path)
    repo = ActivityRepository(store)
    gid = repo.create(request())["activity"]["id"]
    first = repo.act(gid, "RESUME", action(1))
    store.close()

    reopened = SQLiteStore(path)
    try:
        replay = ActivityRepository(reopened).act(gid, "RESUME", action(1))
        assert replay["replayed"] is True and replay["receipt"] == first["receipt"]
        assert replay["activity"]["version"] == 2
        rows = reopened.conn.execute("SELECT operation FROM activity_receipts ORDER BY operation").fetchall()
        assert [row["operation"] for row in rows] == ["CREATE", "RESUME"]
    finally:
        reopened.close()


def test_reading_does_not_write_schema_rows(tmp_path):
    path = tmp_path / "readonly.sqlite"
    store = SQLiteStore(path)
    repo = ActivityRepository(store)
    gid = repo.create(request())["activity"]["id"]
    before = sqlite3.connect(path).execute("SELECT COUNT(*) FROM activity_receipts").fetchone()[0]
    repo.get(gid)
    repo.list()
    store.close()
    after = sqlite3.connect(path).execute("SELECT COUNT(*) FROM activity_receipts").fetchone()[0]
    assert before == after == 1


def test_list_order_is_monotonic_without_clock_resolution(tmp_path):
    """Two creations inside the same clock tick must still order deterministically.

    Windows' timestamp resolution is coarse enough that two separate creates can
    share a microsecond; ordering on the wall clock then fell through to a random
    uuid tie-break. The durable counter is the ordering key, so this is stable.
    """
    store = SQLiteStore(tmp_path / "order.sqlite")
    try:
        repo = ActivityRepository(store)
        ids = [repo.create(request(request_id=f"req-order-{index:04d}",
                                    title=f"第{index}件"))["activity"]["id"]
               for index in range(5)]
        assert [item["id"] for item in repo.list(limit=5)] == list(reversed(ids))
        # An update moves the activity to the front, with a version bump but no
        # dependence on the timestamp value.
        repo.act(ids[0], "RESUME", action(1, "req-order-touch"))
        assert [item["id"] for item in repo.list(limit=5)][0] == ids[0]
        # A separate connection sees the same order, so it is durable state and
        # not an in-process counter.
        reopened_store = SQLiteStore(tmp_path / "order.sqlite")
        try:
            assert [item["id"] for item in ActivityRepository(reopened_store).list(limit=5)][0] == ids[0]
        finally:
            reopened_store.close()
    finally:
        store.close()

