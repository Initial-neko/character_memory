import base64
import json
import shutil
import subprocess
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


VISUAL_CAPTURE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");

// Permission prompts the browser has not answered yet, in request order.
const granted = [];
// Every track the session handed back with stop(), in order.
const stopped = [];
// Intervals the session left running; the preview pipeline is one of them.
const timers = new Map();
let timerSeq = 0;

function makeStream(label) {
  const track = {
    kind: "video",
    label,
    stop() { stopped.push(label); },
    addEventListener() {},
  };
  return {getTracks: () => [track], getVideoTracks: () => [track]};
}

function makeElement() {
  return {
    style: {},
    classList: {add() {}, remove() {}, toggle() {}},
    textContent: "",
    muted: false,
    autoplay: false,
    playsInline: false,
    srcObject: null,
    readyState: 0,
    videoWidth: 0,
    videoHeight: 0,
    width: 0,
    height: 0,
    play() { return Promise.resolve(); },
    pause() {},
    addEventListener() {},
    getContext: () => ({
      drawImage() {},
      getImageData: (x, y, width, height) => ({data: new Uint8ClampedArray(width * height * 4), width, height}),
    }),
    toDataURL: () => "data:image/jpeg;base64,AAAA",
  };
}

const sandbox = {
  document: {createElement: () => makeElement()},
  navigator: {
    mediaDevices: {
      getUserMedia: () => new Promise((resolve, reject) => granted.push({kind: "CAMERA", resolve, reject})),
      getDisplayMedia: () => new Promise((resolve, reject) => granted.push({kind: "DISPLAY", resolve, reject})),
    },
  },
  performance: {now: () => Date.now()},
  setInterval: () => { timerSeq += 1; timers.set(timerSeq, true); return timerSeq; },
  clearInterval: id => { timers.delete(id); },
  setTimeout: () => 0,
  clearTimeout: () => {},
  console: {debug() {}, log() {}, warn() {}, error() {}, info() {}},
};
sandbox.window = sandbox;

vm.runInContext(source, vm.createContext(sandbox), {filename: "visual_capture.js"});
const {createSession} = sandbox.VisualCapture;

// One acquisition that reaches the permission prompt and is answered afterwards.
async function acquire(session, kind, {hangup}) {
  const pending = kind === "CAMERA" ? session.startCamera() : session.startDisplay();
  if (hangup) session.stop({clearCandidates: true, reason: "视觉已关闭"});
  const answer = granted[0];
  answer.resolve(makeStream(kind));
  await pending.catch(() => {});
  return answer;
}

function observe(session, preview, status) {
  return {
    active: session.getState().active,
    source: session.getState().source,
    previewAttached: preview.srcObject !== null,
    timers: timers.size,
    status: status.textContent,
  };
}

async function scenario(kind, {hangup}) {
  granted.length = 0;
  stopped.length = 0;
  timers.clear();
  const preview = makeElement();
  const status = makeElement();
  const session = createSession({preview, status});
  await acquire(session, kind, {hangup});
  return {stopped: stopped.slice(), ...observe(session, preview, status)};
}

async function supersededScenario() {
  granted.length = 0;
  stopped.length = 0;
  timers.clear();
  const preview = makeElement();
  const status = makeElement();
  const session = createSession({preview, status});
  const camera = session.startCamera();      // prompt opened first
  const screen = session.startDisplay();     // the user switched to the screen
  const screenStream = makeStream("DISPLAY");
  granted[1].resolve(screenStream);          // the screen is granted first
  await screen.catch(() => {});
  granted[0].resolve(makeStream("CAMERA"));  // the camera prompt is answered later
  await camera.catch(() => {});
  return {stopped: stopped.slice(), previewIsScreen: preview.srcObject === screenStream, ...observe(session, preview, status)};
}

async function main() {
  process.stdout.write(JSON.stringify({
    cameraAfterHangup: await scenario("CAMERA", {hangup: true}),
    displayAfterHangup: await scenario("DISPLAY", {hangup: true}),
    cameraWhileLive: await scenario("CAMERA", {hangup: false}),
    displayWhileLive: await scenario("DISPLAY", {hangup: false}),
    cameraSupersededByDisplay: await supersededScenario(),
  }));
}

main().catch(error => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
"""


def test_late_permission_answer_cannot_start_a_capture_the_call_no_longer_wants(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to exercise visual_capture.js behaviourally")

    harness = tmp_path / "visual_capture_harness.cjs"
    harness.write_text(VISUAL_CAPTURE_HARNESS, encoding="utf-8")
    script = Path("src/character_memory/web/visual_capture.js").resolve()
    completed = subprocess.run(
        [node, str(harness), str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)

    # Hangup with the permission prompt still open -- voice.js stopCall does exactly this:
    # session.stop(), then voice.visualSession = null. The answer arrives afterwards, with
    # no session left to stop it, so the acquisition itself has to hand the hardware back.
    for source in ("camera", "display"):
        after_hangup = payload[f"{source}AfterHangup"]
        assert after_hangup["stopped"] == [source.upper()], (
            f"{source}: the stream granted after hangup was left live"
        )
        assert after_hangup["active"] is False
        assert after_hangup["source"] is None
        assert after_hangup["previewAttached"] is False, "the preview was started for a dead session"
        assert after_hangup["timers"] == 0, "the sampling timer survived the hangup"
        assert after_hangup["status"] == "视觉已关闭"

    # The same guard must leave the ordinary path untouched.
    for source in ("camera", "display"):
        while_live = payload[f"{source}WhileLive"]
        assert while_live["stopped"] == []
        assert while_live["active"] is True
        assert while_live["source"] == source.upper()
        assert while_live["previewAttached"] is True
        assert while_live["timers"] == 1

    # A newer capture owns the session: the camera answer that lands after the screen was
    # granted must not tear the screen stream down and take its place.
    superseded = payload["cameraSupersededByDisplay"]
    assert superseded["stopped"] == ["CAMERA"]
    assert superseded["source"] == "DISPLAY"
    assert superseded["active"] is True
    assert superseded["previewIsScreen"] is True


def test_server_mounts_capture_routes_after_async_scheduler():
    server = Path("src/character_memory/server.py").read_text(encoding="utf-8")
    assert "attach_visual_routes(app)" in server
    assert "attach_visual_capture_routes(app)" in server
    assert server.index("attach_async_routes(app)") < server.index("attach_visual_capture_routes(app)")
