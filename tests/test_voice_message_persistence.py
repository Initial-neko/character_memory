from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    Event,
    EventType,
    PersonReaction,
)
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.group_web import attach_group_routes
from character_memory.history_web import attach_history_routes
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore
from character_memory.voice_message_fields import (
    VOICE_DURATION_MS,
    VOICE_ERROR,
    VOICE_MEDIA_ID,
    VOICE_STATUS,
    voice_fields,
    voice_pending_fields,
)


def _store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "voice.db")


def _voice_event(character_id="momo") -> Event:
    return Event(
        character_id=character_id,
        event_type=EventType.CHARACTER_MESSAGE,
        event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
        content="晚上好呀",
        metadata={"action": "VOICE_MESSAGE", "action_index": 0, "voice_status": "pending"},
    )


def _ready_voice_metadata() -> dict:
    """A voice message after synthesis reported success."""
    return {
        "action": "VOICE_MESSAGE",
        "action_index": 0,
        VOICE_STATUS: "ready",
        VOICE_MEDIA_ID: "abc123",
        VOICE_DURATION_MS: 1840,
    }


def _api_client(tmp_path, store, character_id="momo") -> TestClient:
    """The real app, with both history route modules attached and backed by `store`.

    The payload builders under test are closures inside attach_history_routes /
    attach_group_routes, so HTTP is the only way to reach them.
    """
    persona_path = tmp_path / "personas" / character_id / "persona.yaml"
    profile = {
        "id": character_id,
        "name": character_id.title(),
        "identity": "",
        "tagline": "",
        "persona_path": str(persona_path),
    }
    settings = SimpleNamespace(
        chat_model="fake",
        embedding_provider="deterministic",
        embedding_model="deterministic",
        db_path=str(tmp_path / "voice.db"),
        base_url="fake",
        persona_path=str(persona_path),
        api_key="",
    )
    app = create_api(bundle=SimpleNamespace(settings=settings, store=store, characters=[profile]))
    attach_history_routes(app)
    attach_group_routes(app)
    return TestClient(app)


def _plain_event(character_id="momo") -> Event:
    return Event(
        character_id=character_id,
        event_type=EventType.CHARACTER_MESSAGE,
        event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
        content="普通消息",
        metadata={"action": "MESSAGE", "action_index": 0},
    )


def test_get_event_round_trips_a_persisted_event(tmp_path):
    store = _store(tmp_path)
    saved = store.append_event(_voice_event())

    reloaded = store.get_event(saved.id)

    assert reloaded.id == saved.id
    assert reloaded.content == "晚上好呀"
    assert reloaded.metadata["voice_status"] == "pending"


def test_get_event_returns_none_for_a_missing_row(tmp_path):
    store = _store(tmp_path)

    assert store.get_event(999999) is None


def test_update_event_metadata_replaces_the_document(tmp_path):
    store = _store(tmp_path)
    saved = store.append_event(_voice_event())

    updated = store.update_event_metadata(
        saved.id,
        {
            "action": "VOICE_MESSAGE",
            "action_index": 0,
            "voice_status": "ready",
            "voice_media_id": "abc123",
            "voice_duration_ms": 1840,
        },
    )

    assert updated is True
    reloaded = store.get_event(saved.id)
    assert reloaded.metadata["voice_status"] == "ready"
    assert reloaded.metadata["voice_media_id"] == "abc123"
    assert reloaded.metadata["voice_duration_ms"] == 1840


def test_update_event_metadata_reports_a_missing_row(tmp_path):
    store = _store(tmp_path)

    assert store.update_event_metadata(999999, {"voice_status": "ready"}) is False


def test_update_event_metadata_leaves_the_row_readable(tmp_path):
    """event_time_epoch is filtered on every read; an UPDATE must not clear it."""
    store = _store(tmp_path)
    saved = store.append_event(_voice_event())

    store.update_event_metadata(saved.id, {"action": "VOICE_MESSAGE", "voice_status": "failed"})

    row = store.conn.execute("SELECT event_time_epoch FROM events WHERE id=?", (saved.id,)).fetchone()
    assert row["event_time_epoch"] is not None
    assert store.get_event(saved.id) is not None


def test_group_update_event_metadata_replaces_the_document(tmp_path):
    """GroupRepository reaches through self.store rather than owning a
    connection of its own, so this is the one place that proxy access could
    silently be written wrong. It also has no caller yet, which is exactly why
    it needs a real test rather than a one-off script."""
    from character_memory.group_store import GroupEvent, GroupRepository

    store = _store(tmp_path)
    repo = GroupRepository(store)
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    group = repo.create_group("测试群", ["momo", "rin"], now)
    saved = repo.append_event(
        GroupEvent(
            conversation_id=group.id,
            turn_id="turn-1",
            actor_type="CHARACTER",
            actor_id="momo",
            event_type="CHARACTER_MESSAGE",
            event_time=now,
            content="晚上好呀",
            metadata={"action": "VOICE_MESSAGE", "voice_status": "pending"},
        )
    )

    assert repo.update_event_metadata(saved.id, {"action": "VOICE_MESSAGE", "voice_status": "ready"}) is True
    assert repo.update_event_metadata(999999, {"voice_status": "ready"}) is False

    # list_events filters on event_time_epoch IS NOT NULL, so reading the row
    # back proves the UPDATE did not clear that column.
    events = repo.list_events(group.id)
    assert events[-1].metadata["voice_status"] == "ready"


ROOT = Path(__file__).resolve().parents[1]


def test_voice_pending_fields_are_the_four_canonical_keys():
    assert voice_pending_fields() == {
        VOICE_STATUS: "pending",
        VOICE_MEDIA_ID: None,
        VOICE_DURATION_MS: None,
        VOICE_ERROR: None,
    }


def test_voice_fields_reads_a_ready_message():
    assert voice_fields(
        {
            "action": "VOICE_MESSAGE",
            VOICE_STATUS: "ready",
            VOICE_MEDIA_ID: "abc123",
            VOICE_DURATION_MS: 1840,
        }
    ) == {
        VOICE_STATUS: "ready",
        VOICE_MEDIA_ID: "abc123",
        VOICE_DURATION_MS: 1840,
        VOICE_ERROR: None,
    }


def test_voice_fields_defaults_to_none_for_a_plain_text_message():
    assert voice_fields({"action": "MESSAGE"}) == {
        VOICE_STATUS: None,
        VOICE_MEDIA_ID: None,
        VOICE_DURATION_MS: None,
        VOICE_ERROR: None,
    }


def test_the_four_wire_key_names_are_pinned():
    """The string values, not just the constants.

    Every other test in the suite (and every caller) uses the constants as dict
    keys, so renaming a constant's *value* leaves the whole suite green while
    every row persisted before the rename silently stops matching: metadata has
    no schema validation, so nothing raises. Pin the wire names themselves.
    """
    assert VOICE_STATUS == "voice_status"
    assert VOICE_MEDIA_ID == "voice_media_id"
    assert VOICE_DURATION_MS == "voice_duration_ms"
    assert VOICE_ERROR == "voice_error"
    assert set(voice_pending_fields()) == {
        "voice_status",
        "voice_media_id",
        "voice_duration_ms",
        "voice_error",
    }
    assert set(voice_fields({})) == set(voice_pending_fields())


def test_both_history_payloads_use_the_shared_projection():
    """Direct and Group payload builders must delegate resource/voice fields."""
    projection = (ROOT / "src/character_memory/message_projection.py").read_text(encoding="utf-8")
    assert "voice_fields(" in projection

    direct = (ROOT / "src/character_memory/history_web.py").read_text(encoding="utf-8")
    group = (ROOT / "src/character_memory/group_web.py").read_text(encoding="utf-8")
    assert "project_direct_message(" in direct
    assert "project_group_message(" in group


def test_both_runtimes_delegate_pending_voice_state_to_the_shared_materializer():
    """The canonical pending fields live behind one Action -> Message boundary."""
    materializer = (ROOT / "src/character_memory/application/action_materialization.py").read_text(encoding="utf-8")
    assert "voice_pending_fields(" in materializer
    for relative in (
        "src/character_memory/runtime/person_runtime.py",
        "src/character_memory/application/group_conversation_service.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "materialize_expressive_action(" in source, relative


def test_a_persisted_voice_message_reloads_with_its_audio_reference(tmp_path):
    """The whole point of this task: a written voice message survives a history
    reload. This drives the real payload builder -- the closure inside
    attach_history_routes that serves GET /v1/chat/history-page -- so dropping
    the spread in that builder turns this red. Calling voice_fields() on the
    reloaded row instead would only re-assert what the store round-trip already
    covers, and would stay green with the builder's spread deleted."""
    store = _store(tmp_path)
    saved = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
            content="晚上好呀",
            metadata={**_ready_voice_metadata(), "source_event_id": 1},
        )
    )
    store.append_event(_plain_event())
    client = _api_client(tmp_path, store)

    response = client.get("/v1/chat/history-page", params={"character_id": "momo"})
    assert response.status_code == 200
    message = next(item for item in response.json()["messages"] if item["id"] == saved.id)

    assert message["voice_status"] == "ready"
    assert message["voice_media_id"] == "abc123"
    assert message["voice_duration_ms"] == 1840
    assert message["voice_error"] is None


def test_a_plain_text_message_gains_the_keys_as_none(tmp_path):
    """Converging the builders adds four keys to every payload; nothing else may
    move. The web client keys off presence, so None (not absence) is the contract."""
    store = _store(tmp_path)
    plain = store.append_event(_plain_event())
    client = _api_client(tmp_path, store)

    message = next(
        item
        for item in client.get("/v1/chat/history-page", params={"character_id": "momo"}).json()["messages"]
        if item["id"] == plain.id
    )

    assert message["content"] == "普通消息"
    assert message["preview"] == "普通消息"
    assert message["voice_status"] is None
    assert message["voice_media_id"] is None
    assert message["voice_duration_ms"] is None
    assert message["voice_error"] is None


def test_the_group_history_payload_carries_the_same_keys(tmp_path):
    """The group builder is a second closure over the same shape; it is reached
    the same way, over GET /v1/groups/{id}/history."""
    store = _store(tmp_path)
    repository = GroupRepository(store)
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    group = repository.create_group("测试群", ["momo", "rin"], now)
    saved = repository.append_event(
        GroupEvent(
            conversation_id=group.id,
            turn_id="turn-1",
            actor_type="CHARACTER",
            actor_id="momo",
            event_type="CHARACTER_MESSAGE",
            event_time=now,
            content="晚上好呀",
            metadata=_ready_voice_metadata(),
        )
    )
    client = _api_client(tmp_path, store)

    response = client.get(f"/v1/groups/{group.id}/history")
    assert response.status_code == 200
    message = next(item for item in response.json()["messages"] if item["id"] == saved.id)

    assert message["voice_status"] == "ready"
    assert message["voice_media_id"] == "abc123"
    assert message["voice_duration_ms"] == 1840
    assert message["voice_error"] is None


def test_the_legacy_history_and_summary_payloads_carry_the_same_keys(tmp_path):
    """api.message_payload is a third copy of the same shape. It serves
    GET /v1/chat/history and -- through character_summary -- the character
    summaries that the browser's unread badge reads."""
    store = _store(tmp_path)
    saved = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
            content="晚上好呀",
            metadata=_ready_voice_metadata(),
        )
    )
    client = _api_client(tmp_path, store)

    legacy = client.get("/v1/chat/history", params={"character_id": "momo"}).json()
    legacy_message = next(item for item in legacy["messages"] if item["id"] == saved.id)
    summaries = client.get("/v1/characters/summaries").json()
    summary_message = summaries["characters"][0]["latest_message"]

    for message in (legacy_message, summary_message):
        assert message["voice_status"] == "ready"
        assert message["voice_media_id"] == "abc123"
        assert message["voice_duration_ms"] == 1840
        assert message["voice_error"] is None


def test_chat_service_history_carries_the_same_keys(tmp_path):
    """ChatService.history is the fourth builder emitting this shape."""
    store = _store(tmp_path)
    saved = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
            content="晚上好呀",
            metadata=_ready_voice_metadata(),
        )
    )
    service = ChatService(store, {}, FixedClock(datetime(2026, 9, 20, tzinfo=timezone.utc)))

    message = next(item for item in service.history("momo")["messages"] if item["id"] == saved.id)

    assert message["voice_status"] == "ready"
    assert message["voice_media_id"] == "abc123"
    assert message["voice_duration_ms"] == 1840
    assert message["voice_error"] is None


class _VoiceActionModel(PersonModel):
    """Drives a runtime into the VOICE_MESSAGE action branch without a provider.

    Returns a canned reaction per call; the model is never asked to synthesize
    anything, which is what keeps synthesis out of this task."""

    def __init__(self, reactions=None):
        self._reactions = list(reactions or [])
        self.contexts: list[str] = []

    def react(self, context):
        return self.react_for_session(context, "default")

    def react_for_session(self, context, session_id):
        self.contexts.append(context)
        if self._reactions:
            return self._reactions.pop(0)
        return _voice_reaction()

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def _voice_reaction(message="  晚上好呀  ") -> PersonReaction:
    return PersonReaction(
        perception="收到",
        reaction="想用语音回一句",
        mental_state_update="平静",
        actions=[ActionDecision(type=ActionType.VOICE_MESSAGE, message=message)],
    )


def _assert_pending_voice_row(row, expected_content="晚上好呀"):
    assert row.content == expected_content, "the text is what the bubble shows when expanded"
    assert row.metadata[VOICE_STATUS] == "pending"
    assert row.metadata[VOICE_MEDIA_ID] is None
    assert row.metadata[VOICE_DURATION_MS] is None
    assert row.metadata[VOICE_ERROR] is None
    assert row.metadata["action"] == "VOICE_MESSAGE"


def test_the_direct_runtime_writes_the_pending_state(tmp_path):
    """The write branch at person_runtime.py, exercised through handle() rather
    than asserted against the source text. Nothing else in the suite reaches it,
    which is how a silently dropped metadata.update would go unnoticed."""
    store = _store(tmp_path)
    embeddings = DeterministicEmbedding()
    runtime = PersonRuntime(
        store,
        VectorRecall(store, embeddings),
        embeddings,
        _VoiceActionModel(),
        "persona",
    )

    runtime.handle(
        Event(
            character_id="momo",
            event_type=EventType.USER_MESSAGE,
            event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
            content="在吗",
            metadata={"conversation_id": "voice-check"},
        )
    )

    rows = [item for item in store.list_events("momo") if item.event_type == EventType.CHARACTER_MESSAGE]
    assert len(rows) == 1
    _assert_pending_voice_row(rows[0])
    store.close()


def test_the_group_runtime_writes_the_pending_state(tmp_path):
    """The second write branch, in group_conversation_service's action loop."""
    from character_memory.application.group_conversation_service import GroupConversationService

    store = _store(tmp_path)
    embeddings = DeterministicEmbedding()
    model = _VoiceActionModel()
    runtimes = {
        "momo": PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "你是 Momo。"),
        "rin": PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "你是 Rin。"),
    }
    service = GroupConversationService(
        store,
        runtimes,
        FixedClock(datetime(2026, 9, 20, tzinfo=timezone.utc)),
        profiles=[{"id": "momo", "name": "Momo"}, {"id": "rin", "name": "Rin"}],
    )
    group = service.create_group("测试群", ["momo", "rin"])

    service.send(group.id, "在吗")

    rows = [item for item in service.repo.list_events(group.id) if item.actor_type == "CHARACTER"]
    assert len(rows) == 2
    for row in rows:
        _assert_pending_voice_row(row)
    store.close()
