from datetime import datetime, timezone
import json
from pathlib import Path

from character_memory.application.async_conversation import ConversationEventHub, ReactionScheduler, direct_channel
from character_memory.application.voice_message_materializer import (
    VoiceMessageMaterializer,
    _response_detail,
)
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
        self.text = ""

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


def test_direct_response_publisher_materializes_proactive_voice_messages(tmp_path):
    store = SQLiteStore(tmp_path / "proactive-publish.db")
    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    source = store.append_event(Event(
        character_id="momo",
        event_type=EventType.PROACTIVE_INTENT,
        event_time=now,
        content="到点后重新判断",
        metadata={"conversation_id":"browser-session"},
    ))
    voice = store.append_event(Event(
        character_id="momo",
        event_type=EventType.CHARACTER_MESSAGE,
        event_time=now,
        content="我用语音提醒你一下。",
        metadata={
            "action":"VOICE_MESSAGE",
            "source_event_id":source.id,
            "source_event_type":"PROACTIVE_INTENT",
            "conversation_id":"browser-session",
            **voice_pending_fields(),
        },
    ))

    class _Recorder:
        def __init__(self):
            self.calls = []

        def materialize_direct(self, event, *, conversation_id):
            self.calls.append((event.id, conversation_id))

    recorder = _Recorder()
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(lambda: None, lambda: [], hub, voice_materializer=recorder)
    try:
        published = scheduler.publish_direct_responses(
            store,
            "momo",
            "browser-session",
            source.id,
        )
        assert [item["id"] for item in published] == [voice.id]
        assert recorder.calls == [(voice.id, "browser-session")]
        channel_events = list(hub._channel(direct_channel("momo", "browser-session")).events)
        assert channel_events[-1][1] == "character_event"
        assert channel_events[-1][2]["metadata"]["voice_status"] == "pending"
    finally:
        scheduler.close()
        hub.close()
        store.close()


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


class _ErrorResponse:
    """The shape the media runtime answers a refused synthesis with.

    Copied from the live stack: every hop between the materializer and the
    sidecar replies with the previous hop's body verbatim, so the sentence that
    names the cause arrives nested inside escaped JSON.
    """

    def __init__(self, detail):
        self.status_code = 503
        self.content = b""
        self.headers = {}
        self._detail = detail

    @property
    def text(self):
        return json.dumps({"detail": self._detail})

    def json(self):
        return {"detail": self._detail}


def _refused(detail):
    return _ErrorResponse(detail)


def test_a_failed_synthesis_keeps_the_cause_the_body_carried(tmp_path):
    """ "503 Service Unavailable for url ..." names the hop, never the reason.

    That string was the whole of what a failed voice message recorded, so the
    bubble could say a voice message had failed and nothing else -- while the
    sentence naming the missing template sat in the body one layer down.
    """
    store = SQLiteStore(tmp_path / "voice.db")
    hub = _Hub()
    inner = "Default template 'murasame' is not defined; pick an existing template in Settings Center."
    client = _Client(response=_refused(json.dumps({"detail": inner})))
    event = store.append_event(Event(
        character_id="momo",
        event_type=EventType.CHARACTER_MESSAGE,
        event_time=datetime(2026, 9, 21, 23, 1, tzinfo=timezone.utc),
        content="喂喂，听得到吗～",
        metadata={"action": "VOICE_MESSAGE", "conversation_id": "momo", **voice_pending_fields()},
    ))

    _materializer(tmp_path, store, hub, client).materialize_direct(event, conversation_id="momo")

    saved = store.get_event(event.id)
    assert saved.metadata["voice_status"] == "failed"
    stored = saved.metadata["voice_error"]
    assert inner in stored, stored
    assert "503" in stored and "http://127.0.0.1:8001/v1/tts" in stored, stored
    assert saved.metadata["voice_media_id"] is None


def test_a_failed_synthesis_falls_back_to_the_body_when_it_is_not_json(tmp_path):
    """A proxy or a crash answers with plain text; that is still the reason."""

    class _Plain(_ErrorResponse):
        @property
        def text(self):
            return "upstream connect error"

        def json(self):
            raise ValueError("not json")

    assert _response_detail(_Plain("")) == "upstream connect error"


def test_the_failure_bubble_shows_the_reason_it_stored():
    """Stored and rendered are different things, and only one of them happened.

    ``voice_error`` reached the payload from the day the field existed -- in
    both renderers -- and no markup read it, so a failed voice message said
    only that it had failed.
    """
    root = Path(__file__).resolve().parents[1]
    app = (root / "src/character_memory/web/app.js").read_text(encoding="utf-8")
    css = (root / "src/character_memory/web/styles.css").read_text(encoding="utf-8")

    assert "message.voice_error" in app
    assert 'class="voice-error"' in app
    assert ".voice-error" in css
