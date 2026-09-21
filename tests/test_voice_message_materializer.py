from datetime import datetime, timezone
from pathlib import Path

from character_memory.application.voice_message_materializer import VoiceMessageMaterializer
from character_memory.domain.models import Event, EventType
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.media import MediaStorage
from character_memory.storage.sqlite import SQLiteStore
from character_memory.voice_message_fields import voice_pending_fields


WAV = b"RIFF" + (b"\x00" * 4) + b"WAVEfmt " + (b"\x00" * 24)


class _Hub:
    def __init__(self):
        self.published = []

    def publish(self, channel, event_type, data):
        self.published.append((channel, event_type, data))


class _Response:
    def __init__(self, content=WAV, *, status=200):
        self.content = content
        self.status_code = status
        self.headers = {"x-media-audio-ms": "3210"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, response=None):
        self.response = response or _Response()
        self.calls = []

    def post(self, url, json):
        self.calls.append((url, json))
        return self.response


def _materializer(tmp_path, store, hub, client):
    return VoiceMessageMaterializer(
        lambda: store,
        MediaStorage(tmp_path / "media"),
        hub,
        media_base="http://127.0.0.1:8001",
        client=client,
    )


def test_direct_voice_message_is_one_tts_request_and_one_persisted_asset(tmp_path):
    store = SQLiteStore(tmp_path / "voice.db")
    hub = _Hub()
    client = _Client()
    text = "第一句。第二句。第三句。"
    event = store.append_event(Event(
        character_id="momo",
        event_type=EventType.CHARACTER_MESSAGE,
        event_time=datetime(2026, 9, 21, tzinfo=timezone.utc),
        content=text,
        metadata={"action":"VOICE_MESSAGE","conversation_id":"c1",**voice_pending_fields()},
    ))

    _materializer(tmp_path, store, hub, client).materialize_direct(event, conversation_id="c1")

    assert client.calls == [("http://127.0.0.1:8001/v1/tts", {"text": text, "voice": "momo"})]
    saved = store.get_event(event.id)
    assert saved.metadata["voice_status"] == "ready"
    assert saved.metadata["voice_duration_ms"] == 3210
    asset = store.get_media_asset(saved.metadata["voice_media_id"])
    assert asset is not None
    assert asset.source == "VOICE_MESSAGE"
    assert (tmp_path / "media" / asset.storage_name).read_bytes() == WAV


def test_direct_voice_message_failure_keeps_text_and_marks_failed(tmp_path):
    store = SQLiteStore(tmp_path / "voice.db")
    hub = _Hub()
    client = _Client(_Response(status=503))
    event = store.append_event(Event(
        character_id="momo",
        event_type=EventType.CHARACTER_MESSAGE,
        event_time=datetime(2026, 9, 21, tzinfo=timezone.utc),
        content="别担心，我在。",
        metadata={"action":"VOICE_MESSAGE","conversation_id":"c1",**voice_pending_fields()},
    ))

    _materializer(tmp_path, store, hub, client).materialize_direct(event, conversation_id="c1")

    saved = store.get_event(event.id)
    assert saved.content == "别担心，我在。"
    assert saved.metadata["voice_status"] == "failed"
    assert saved.metadata["voice_media_id"] is None


def test_group_voice_message_updates_the_same_group_event(tmp_path):
    store = SQLiteStore(tmp_path / "voice.db")
    repo = GroupRepository(store)
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    group = repo.create_group("群", ["momo", "rin"], now)
    event = repo.append_event(GroupEvent(
        conversation_id=group.id,
        turn_id="t1",
        actor_type="CHARACTER",
        actor_id="momo",
        event_type="CHARACTER_MESSAGE",
        event_time=now,
        content="我用语音说。",
        metadata={"action":"VOICE_MESSAGE",**voice_pending_fields()},
    ))
    hub = _Hub()
    client = _Client()
    raw = {
        "id":event.id,"conversation_id":group.id,"turn_id":"t1",
        "actor_type":"CHARACTER","actor_id":"momo","event_type":"CHARACTER_MESSAGE",
        "event_time":now.isoformat(),"content":event.content,"metadata":event.metadata,
    }

    _materializer(tmp_path, store, hub, client).materialize_group(raw)

    saved = repo.list_events(group.id)[-1]
    assert saved.id == event.id
    assert saved.metadata["voice_status"] == "ready"
    assert hub.published[-1][1] == "group_character_event"
    assert hub.published[-1][2]["id"] == event.id


def test_voice_message_frontend_has_native_bubble_not_audio_controls():
    root = Path(__file__).resolve().parents[1]
    app = (root / "src/character_memory/web/app.js").read_text(encoding="utf-8")
    groups = (root / "src/character_memory/web/groups.js").read_text(encoding="utf-8")
    css = (root / "src/character_memory/web/styles.css").read_text(encoding="utf-8")

    assert 'message.action !== "VOICE_MESSAGE"' in app
    assert "voice-bubble" in app
    assert "new Audio(" in app
    assert "<audio controls" not in app
    assert "voice_status:metadata.voice_status" in app
    assert "voice_status:metadata.voice_status" in groups
    assert "CM.voiceMessageHtml(message)" in groups
    assert ".voice-bubble" in css
