"""Space source facts keep an explicit experience scope across storage and events."""
from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

import pytest

from character_memory.domain.experience_scope import is_real_experience
from character_memory.domain.models import EventType, PersonReaction
from character_memory.space_autonomy import SpaceAutonomyService
from character_memory.space_store import SpaceRepository
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
REAL = {"schema_version": 1, "reality": "REAL"}
SIM = {"schema_version": 1, "reality": "SIMULATED", "activity_id": "practice", "branch_id": "other"}
BAD = [
    {},
    "REAL",
    True,
    [],
    {"reality": "REAL"},
    {"schema_version": True, "reality": "REAL"},
    {"schema_version": 2, "reality": "REAL"},
    {"schema_version": 1, "reality": "PRACTICE"},
    {**REAL, "branch_id": "ambiguous"},
]


class RecordingRuntime:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)
        return SimpleNamespace(reaction=PersonReaction(actions=[]))


def _access(store, ids=("c00", "c01")):
    runtimes = {character_id: RecordingRuntime() for character_id in ids}
    bundle = SimpleNamespace(runtimes=runtimes)
    access = SimpleNamespace(
        settings=SimpleNamespace(
            api_key="test-key",
            space_audience_size=5,
            space_thread_reply_rounds=1,
            space_media_enabled=False,
            space_media_max_items=0,
        ),
        read_store=store,
        store=lambda: store,
        character_profiles=lambda: [{"id": item, "name": item} for item in ids],
        require_bundle=lambda: bundle,
    )
    return access, runtimes


def test_source_facts_round_trip_explicit_and_legacy_scopes(tmp_path):
    path = tmp_path / "scope.db"
    store = SQLiteStore(path)
    repository = SpaceRepository(store)

    real = repository.create_post("c00", "real post", NOW, experience_scope=REAL)
    sim = repository.create_post("c00", "simulated post", NOW, experience_scope=SIM)
    legacy = repository.create_post("c00", "legacy post", NOW)
    assert repository.get_post(real.id).experience_scope == REAL
    assert repository.get_post(sim.id).experience_scope == SIM
    assert repository.get_post(legacy.id).experience_scope is None

    real_comment = repository.add_comment(real.id, "c01", "real comment", NOW, experience_scope=REAL)
    sim_comment = repository.add_comment(sim.id, "c01", "sim comment", NOW, experience_scope=SIM)
    legacy_comment = repository.add_comment(legacy.id, "c01", "legacy comment", NOW)
    assert repository.get_comment(real_comment.id).experience_scope == REAL
    assert repository.get_comment(sim_comment.id).experience_scope == SIM
    assert repository.get_comment(legacy_comment.id).experience_scope is None

    store.close()
    # Persisted scopes survive a reopen rather than being inferred from context.
    store = SQLiteStore(path)
    repository = SpaceRepository(store)
    assert repository.get_post(real.id).experience_scope == REAL
    assert repository.get_post(sim.id).experience_scope == SIM
    assert repository.get_post(legacy.id).experience_scope is None
    assert repository.get_comment(sim_comment.id).experience_scope == SIM
    store.close()


@pytest.mark.parametrize("scope", BAD)
def test_malformed_or_unknown_scope_is_rejected_on_write(tmp_path, scope):
    store = SQLiteStore(tmp_path / "bad.db")
    repository = SpaceRepository(store)
    post = repository.create_post("c00", "real post", NOW)
    with pytest.raises(ValueError):
        repository.create_post("c00", "bad post", NOW, experience_scope=scope)
    with pytest.raises(ValueError):
        repository.add_comment(post.id, "c01", "bad comment", NOW, experience_scope=scope)
    assert repository.list_comments(post.id) == []
    store.close()


def test_corrupt_stored_scope_reads_fail_closed(tmp_path):
    store = SQLiteStore(tmp_path / "corrupt.db")
    repository = SpaceRepository(store)
    post = repository.create_post("c00", "post", NOW, experience_scope=SIM)
    comment = repository.add_comment(post.id, "c01", "comment", NOW, experience_scope=SIM)
    store.conn.execute("UPDATE space_posts SET experience_scope_json=? WHERE id=?", ("{not json", post.id))
    store.conn.execute("UPDATE space_comments SET experience_scope_json=? WHERE id=?", ("[]", comment.id))
    store.conn.commit()

    assert repository.get_post(post.id).experience_scope == {}
    assert repository.get_comment(comment.id).experience_scope == {}
    assert not is_real_experience({"experience_scope": repository.get_post(post.id).experience_scope})
    store.close()


def test_post_seen_event_inherits_source_scope_and_legacy_stays_legacy(tmp_path):
    store = SQLiteStore(tmp_path / "events.db")
    repository = SpaceRepository(store)
    access, runtimes = _access(store)
    service = SpaceAutonomyService(access, repository)

    for scope in (REAL, SIM, None):
        post = repository.create_post("c00", "post", NOW, experience_scope=scope)
        service.process_audience(post.id, now=NOW)
        event = runtimes["c01"].events[-1]
        assert event.event_type == EventType.SPACE_POST_SEEN
        assert event.metadata["post_id"] == post.id
        if scope is None:
            # Legacy facts stay legacy-real; no default REAL scope is written.
            assert "experience_scope" not in event.metadata
            assert is_real_experience(event.metadata)
        else:
            assert event.metadata["experience_scope"] == scope
            assert is_real_experience(event.metadata) is (scope == REAL)
    store.close()


def test_comment_received_event_inherits_comment_scope(tmp_path):
    store = SQLiteStore(tmp_path / "comment-events.db")
    repository = SpaceRepository(store)
    access, runtimes = _access(store)
    service = SpaceAutonomyService(access, repository)

    for scope in (REAL, SIM, None):
        post = repository.create_post("c00", "post", NOW)
        comment = repository.add_comment(post.id, "c01", "comment", NOW, experience_scope=scope)
        service.process_comment_thread(post.id, comment.id, now=NOW)
        event = runtimes["c00"].events[-1]
        assert event.event_type == EventType.SPACE_COMMENT_RECEIVED
        assert event.metadata["comment_id"] == comment.id
        if scope is None:
            assert "experience_scope" not in event.metadata
            assert is_real_experience(event.metadata)
        else:
            assert event.metadata["experience_scope"] == scope
            assert is_real_experience(event.metadata) is (scope == REAL)
    store.close()


def test_legacy_database_starts_and_keeps_legacy_rows(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE space_posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_at_epoch INTEGER NOT NULL,
            media_id TEXT,
            source_event_id INTEGER,
            visibility TEXT NOT NULL DEFAULT 'ACTIVE_CHARACTERS'
        );
        CREATE TABLE space_comments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            character_id TEXT NOT NULL,
            actor_type TEXT NOT NULL DEFAULT 'CHARACTER',
            content TEXT NOT NULL,
            sticker_id TEXT,
            created_at TEXT NOT NULL,
            created_at_epoch INTEGER NOT NULL,
            reply_to_comment_id INTEGER,
            mentions_json TEXT NOT NULL DEFAULT '[]',
            mentions_user INTEGER NOT NULL DEFAULT 0,
            client_request_id TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO space_posts(character_id,content,created_at,created_at_epoch) VALUES(?,?,?,?)",
        ("c00", "legacy post", NOW.isoformat(), 0),
    )
    conn.execute(
        "INSERT INTO space_comments(post_id,character_id,content,created_at,created_at_epoch) VALUES(?,?,?,?,?)",
        (1, "c01", "legacy comment", NOW.isoformat(), 0),
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    repository = SpaceRepository(store)
    columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(space_posts)")}
    assert "experience_scope_json" in columns
    assert repository.get_post(1).experience_scope is None
    assert repository.get_comment(1).experience_scope is None

    # The upgraded database accepts explicit scopes for new facts.
    post = repository.create_post("c00", "new post", NOW, experience_scope=SIM)
    assert repository.get_post(post.id).experience_scope == SIM
    store.close()
