import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from character_memory.application.async_conversation import ConversationEventHub, ReactionScheduler, _PendingState
from character_memory.config import Settings
from character_memory.dev_server import create_dev_app
from character_memory.visual_capture_web import DirectVisualMessageRequest, VisualFrameRequest, normalize_visual_frames


def _jpeg_data_url(payload: bytes = b"\xff\xd8\xffvisual-frame") -> str:
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")


def test_visual_frame_contract_caps_one_turn_at_five_frames():
    frame = {"filename": "frame.jpg", "data_url": _jpeg_data_url(), "source": "CAMERA"}
    request = DirectVisualMessageRequest(message="你看一下", visual_frames=[frame] * 5)
    assert len(request.visual_frames) == 5
    with pytest.raises(ValidationError):
        DirectVisualMessageRequest(message="太多了", visual_frames=[frame] * 6)


def test_visual_frames_are_validated_as_transient_context():
    frame = VisualFrameRequest(data_url=_jpeg_data_url(), source="DISPLAY", captured_at_ms=1234)
    urls, metadata = normalize_visual_frames([frame])
    assert urls == [_jpeg_data_url()]
    assert metadata == {
        "frame_count": 1,
        "sources": ["DISPLAY"],
        "captured_at_ms": {"first": 1234, "last": 1234},
    }


def test_scheduler_keeps_multiple_images_for_one_event():
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(lambda: None, lambda: [], hub, quiet_seconds=0, max_burst_seconds=0)
    state = _PendingState()
    state.latest_event = SimpleNamespace(id=2)
    state.image_urls = {1: ["frame-a", "frame-b"], 2: ["frame-c"]}
    state.pending_since = 0.0
    state.last_submit_at = 0.0
    snapshot = scheduler._snapshot_after_quiet(state)
    assert snapshot is not None
    _event, watermark, image_urls, _mentions = snapshot
    assert watermark == 2
    assert image_urls == ["frame-a", "frame-b", "frame-c"]
    scheduler.close()
    hub.close()


def test_visual_capture_assets_cover_camera_display_keyframes_and_call_persistence():
    index = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    capture = Path("src/character_memory/web/visual_capture.js").read_text(encoding="utf-8")
    voice = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    css = Path("src/character_memory/web/visual_capture.css").read_text(encoding="utf-8")

    assert "/static/visual_capture.js" in index
    assert "/static/visual_capture.css" in index
    assert 'id="voiceMicButton"' in index
    assert 'id="voiceCameraButton"' in index
    assert 'id="voiceScreenButton"' in index
    assert 'id="voiceVisualPreview"' in index
    assert "getUserMedia" in capture
    assert 'facingMode:{ideal:"user"}' in capture
    assert "getDisplayMedia" in capture
    assert 'displaySurface:"window"' in capture
    assert "selectFrames" in capture
    assert "maxFrames = 4" in capture
    assert "maxCandidates: 18" in capture
    assert "/v1/visual/direct/messages" in voice
    assert "/v1/visual/groups/" in voice
    assert "visual_frames" in voice
    assert "micActive" in voice
    assert "stopMicrophone" in voice
    assert "sendTextWithVisual" in voice
    assert "visualFramesForCurrentConversation" in voice
    assert "fromMs:Math.max(0, now - 15000)" in voice
    assert "voice.visualSession?.stop" in voice
    minimize_block = voice.split("function minimizeCall()", 1)[1].split("function expandCall()", 1)[0]
    assert "visualSession" not in minimize_block
    assert ".voice-call-dock-visual" in css


def test_voice_uses_only_confirmed_female_speaker_pool():
    voice = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert "const TTS_SPEAKER_IDS = [0, 2, 5];" in voice
    assert "TTS_SPEAKER_IDS[(hash >>> 0) % TTS_SPEAKER_IDS.length]" in voice
    assert "TTS_SPEAKER_COUNT" not in voice


class FakeVisionModel:
    model = "fake-chat"
    vision_model = "fake-vision"

    def __init__(self):
        self.calls = []

    def _request(self, messages, *, conversation_id=None, json_object=False, model=None):
        self.calls.append({"messages": messages, "conversation_id": conversation_id, "model": model})
        return "我看到了测试画面"

    def close(self):
        pass


def _settings() -> Settings:
    return Settings(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        chat_model="fake-chat",
        vision_model="fake-vision",
        embedding_provider="deterministic",
    )


def test_dev_console_injects_visual_capture_and_calls_real_vision_path():
    html = Path("src/character_memory/web/dev.html").read_text(encoding="utf-8")
    script = Path("src/character_memory/web/dev_capture.js").read_text(encoding="utf-8")
    assert "Visual Capture + Vision" in html
    assert "ImageGen" in html
    assert 'id="visualPreview"' in html
    assert 'id="runVision"' in html
    assert "/static/visual_capture.js" in html
    assert "/static/dev_capture.js" in html
    assert "/v1/dev/vision" in script

    model = FakeVisionModel()
    app = create_dev_app(settings=_settings(), model_factory=lambda _: model)
    with TestClient(app) as client:
        response = client.post(
            "/v1/dev/vision",
            json={
                "prompt": "画面里有什么？",
                "system_prompt": "只看图回答。",
                "image_data_urls": [_jpeg_data_url(), _jpeg_data_url(b"\xff\xd8\xffsecond")],
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["kind"] == "vision"
    assert data["model"] == "fake-vision"
    assert data["frame_count"] == 2
    assert data["reply"] == "我看到了测试画面"
    assert model.calls[0]["conversation_id"] == "dev-console-vision"
    assert model.calls[0]["model"] == "fake-vision"
    content = model.calls[0]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "画面里有什么？"}
    assert [item["type"] for item in content[1:]] == ["image_url", "image_url"]


def test_server_mounts_capture_routes_after_async_scheduler():
    server = Path("src/character_memory/server.py").read_text(encoding="utf-8")
    assert "attach_visual_routes(app)" in server
    assert "attach_visual_capture_routes(app)" in server
    assert server.index("attach_async_routes(app)") < server.index("attach_visual_capture_routes(app)")
