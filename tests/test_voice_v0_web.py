import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_voice_assets_are_wired_into_chat_shell():
    html = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    assert 'id="voiceCallButton"' in html
    assert 'id="voiceCallOverlay"' in html
    assert 'id="voiceCallLog"' in html
    assert 'id="voiceMinimizeButton"' in html
    assert 'id="voiceCallDock"' in html
    assert 'id="voiceDockExpandButton"' in html
    assert '当前对话' in html
    assert '/static/voice.css' in html
    assert '/static/voice.js' in html


def test_voice_reuses_existing_async_chat_and_sse_contracts_for_direct_and_group():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert '"/v1/chat/messages"' in script
    assert '/v1/groups/${encodeURIComponent(target.conversationId)}/messages' in script
    assert '"/v1/visual/direct/messages"' in script
    assert '/v1/visual/groups/${encodeURIComponent(target.conversationId)}/messages' in script
    assert 'new EventSource(`/v1/events/stream?' in script
    assert 'source.addEventListener("character_event"' in script
    assert 'source.addEventListener("group_character_event"' in script
    assert '/v1/asr' in script
    assert '/v1/tts' in script


def test_voice_call_keeps_capture_live_during_playback_without_barge_in_or_webrtc():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert '["listening", "recording"].includes(voice.capturePhase)' in script
    assert 'setCapturePhase("listening")' in script
    assert 'setPhase("speaking"' in script
    stop_body = script.split('async function stopCall()', 1)[1].split('dom.button?.addEventListener', 1)[0]
    assert 'audio.pause()' in stop_body
    assert 'audio.onended?.()' in stop_body
    assert 'audio.pause()' not in script.split('function audioFrame', 1)[1].split('async function startCall', 1)[0]
    assert 'RTCPeerConnection' not in script
    assert 'getDisplayMedia' not in script
    assert 'requestSubmit' not in script


def test_voice_call_pins_target_and_survives_conversation_switches():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'voice.target = target' in script
    assert 'function captureTarget()' in script
    assert 'function isViewingTarget()' in script
    assert 'CM.on("conversationChanged", () => { if (voice.active) renderCallIdentity(); });' in script
    assert 'CM.on("conversationChanged", () => { if (voice.active) stopCall(); });' not in script


def test_voice_call_can_mute_microphone_without_stopping_visual_or_tts():
    index = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    app = Path("src/character_memory/web/app.js").read_text(encoding="utf-8")
    groups = Path("src/character_memory/web/groups.js").read_text(encoding="utf-8")

    assert 'id="voiceMicButton"' in index
    assert "micActive: false" in script
    assert "async function startMicrophone" in script
    assert "async function stopMicrophone" in script
    assert "async function toggleMicrophone" in script
    stop_mic = script.split("async function stopMicrophone", 1)[1].split("async function startMicrophone", 1)[0]
    assert "visualSession?.stop" not in stop_mic
    assert "eventSource?.close" not in stop_mic
    assert "currentAudio" not in stop_mic
    start_call = script.split("async function startCall()", 1)[1].split("async function stopCall()", 1)[0]
    assert "voice.active = true" in start_call
    assert "startMicrophone({throwOnError:false})" in start_call
    assert start_call.index("voice.active = true") < start_call.index("startMicrophone({throwOnError:false})")
    assert "sendTextWithVisual" in script
    assert "CM.features.voice?.sendTextWithVisual?.(message)" in app
    assert "CM.features.voice?.sendTextWithVisual?.(message)" in groups


def test_voice_call_can_minimize_without_stopping_capture():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'function minimizeCall()' in script
    assert 'function expandCall()' in script
    assert 'dom.overlay?.classList.add("hidden")' in script
    assert 'dom.dock?.classList.remove("hidden")' in script
    minimize_body = script.split('function minimizeCall()', 1)[1].split('function expandCall()', 1)[0]
    assert 'getTracks' not in minimize_body
    assert 'audioContext' not in minimize_body
    assert 'visualSession' not in minimize_body


def test_voice_group_tts_uses_current_character_speaker_avatar_and_prefetch_slot():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'voice.queue.push({text, characterId, messageId:data.id, audioPromise:null, audioUrl:null})' in script
    assert 'voice.currentSpeakerId = item.characterId' in script
    assert 'const speakerId = stableSpeakerId(item.characterId)' in script
    assert 'speaker_id:speakerId' in script
    assert 'function prefetchNext()' in script
    assert 'profile?.avatar_url' in script
    assert 'voice-call-avatar-image' in script


def test_voice_exposes_latency_breakdown_in_ui():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'ASR ${Math.round(m.asr)}ms' in script
    assert 'LLM ${Math.round(m.llm)}ms' in script
    assert 'TTS ${Math.round(m.tts)}ms' in script
    assert '总计 ${Math.round(m.total)}ms' in script


def test_voice_keeps_live_call_text_visible_and_direct_chat_sync_is_scoped():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'appendCallLog("user"' in script
    assert 'appendCallLog("assistant"' in script
    assert 'voice.target?.scope === "direct" && isViewingTarget()' in script
    assert 'CM.mergeDirectMessage(CM.directEventToMessage(data))' in script
    assert 'CM.features.groups?.reconcileLatest?.(target.conversationId)' in script


def test_voice_asr_gate_rejects_empty_punctuation_and_low_information_before_queue_or_send():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'function validateAsrTranscript(raw)' in script
    assert 'reason:"empty"' in script
    assert 'text.replace(/[\\s\\p{P}\\p{S}]/gu, "")' in script
    assert '/\\p{Script=Han}/u.test(text)' in script
    assert 'reason:"punctuation_only"' in script
    # A Latin-only answer is the hallucination shape on non-speech, so the gate no
    # longer admits one: `no_han` replaced the `>= 2 Latin characters` rule.
    assert 'reason:"no_han"' in script
    assert 'latinOrDigitCount' not in script

    finish_speech = script.split('async function finishSpeech()', 1)[1].split('async function synthesize', 1)[0]
    assert 'const validation = validateAsrTranscript(result.text);' in finish_speech
    invalid_block = finish_speech.split('if (!validation.valid) {', 1)[1].split('const turn = {text:validation.text, visualFrames, asrMs};', 1)[0]
    assert '没有识别到有效内容' in invalid_block
    assert 'setPhase("listening", "正在听…")' in invalid_block
    assert 'return;' in invalid_block
    assert 'sendTranscript' not in invalid_block
    assert finish_speech.index('const validation = validateAsrTranscript(result.text);') < finish_speech.index('const turn = {text:validation.text, visualFrames, asrMs};')


def test_voice_buffers_valid_asr_during_tts_and_submits_after_queue_drain():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    finish_speech = script.split('async function finishSpeech()', 1)[1].split('async function synthesize', 1)[0]
    play_queue = script.split('async function playQueue()', 1)[1].split('function audioFrame', 1)[0]

    assert 'if (voice.playing || voice.queue.length)' in finish_speech
    assert 'voice.pendingTurns.push(turn)' in finish_speech
    assert '已听到，等待对方说完' in finish_speech
    assert 'await flushPendingTurns();' in play_queue
    assert 'await playAudio(url);' in play_queue


def test_voice_css_has_explicit_contrast_and_dock_styles():
    css = Path("src/character_memory/web/voice.css").read_text(encoding="utf-8")
    assert '.voice-call-card' in css and 'color: #f6f7fb' in css
    assert '.voice-call-dock {' in css
    assert '.voice-call-dock-avatar' in css
    assert '.voice-call-avatar-image' in css


def test_voice_does_not_modify_core_controller_ownership():
    app = Path("src/character_memory/web/app.js").read_text(encoding="utf-8")
    voice = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'id="voiceCallButton"' not in app
    assert 'CM.registerFeature("voice"' in voice


# The microphone button is a real permission prompt, which is the only thing that makes these
# interleavings reachable: the browser answers whenever the user gets around to it, and by then
# the call may already be over. The harness parks /health and getUserMedia on promises the
# scenario releases by hand, so the "prompt is still open" window can be held open on command.
VOICE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");

let granted = [];
let stopped = [];
let alerts = [];
let healthWaiters = [];
let contextsOpened = 0;
let elements = new Map();

const VOICE_ELEMENT_IDS = [
  "voiceCallButton", "voiceCallOverlay", "voiceCallAvatar", "voiceCallName", "voiceCallContext",
  "voiceCallStatus", "voiceCallTranscript", "voiceCallLog", "voiceCallMetrics", "voiceMinimizeButton",
  "voiceHangupButton", "voiceCallDock", "voiceDockExpandButton", "voiceDockAvatar", "voiceDockTitle",
  "voiceDockStatus", "voiceDockVisual", "voiceDockHangupButton", "voiceMicButton", "voiceCameraButton",
  "voiceScreenButton", "voiceVisualStopButton", "voiceVisualPanel", "voiceVisualPreview", "voiceVisualStatus",
];

function makeElement(id) {
  const classes = new Set();
  const attributes = {};
  const handlers = [];
  return {
    id,
    disabled: false,
    textContent: "",
    title: "",
    innerHTML: "",
    scrollTop: 0,
    scrollHeight: 0,
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      toggle: (name, on) => { if (on) classes.add(name); else classes.delete(name); },
      contains: name => classes.has(name),
    },
    setAttribute(name, value) { attributes[name] = value; },
    getAttribute(name) { return attributes[name]; },
    addEventListener(type, handler, options) { handlers.push({type, handler, capture: Boolean(options && options.capture)}); },
    handlers: () => handlers.slice(),
    querySelector() { return null; },
    append() {},
    appendChild() {},
    remove() {},
    focus() {},
    pause() {},
    dispatchEvent() { return true; },
  };
}

function boot() {
  granted = [];
  stopped = [];
  alerts = [];
  healthWaiters = [];
  contextsOpened = 0;
  elements = new Map();

  function elementFor(id) {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  }
  for (const id of VOICE_ELEMENT_IDS) elementFor(id);

  const CM = {
    dom: {input: elementFor("messageInput")},
    state: {
      characters: [{id: "rin", name: "Rin"}],
      characterId: "rin",
      conversation: {type: "DIRECT", characterId: "rin", groupId: null},
    },
    events: {},
    features: {groups: {current: () => null}},
    on(name, handler) { CM.events[name] = handler; },
    registerFeature(name, feature) { CM.features[name] = feature; return feature; },
    isGroupConversation() { return false; },
    currentProfile() { return CM.state.characters[0]; },
    conversationIdFor(characterId) { return "direct:" + characterId; },
    directEventToMessage() { return {}; },
    mergeDirectMessage() {},
  };

  const sandbox = {
    console: {debug() {}, log() {}, warn() {}, error() {}, info() {}},
    localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
    alert: message => alerts.push(message),
    // The error paths schedule a status reset 1.2s out; the scenarios below never wait for it.
    setTimeout: () => 0,
    clearTimeout() {},
    performance: {now: () => Date.now()},
    URLSearchParams,
    fetch: async url => {
      if (String(url).endsWith("/health")) {
        return new Promise(resolve => healthWaiters.push(() => resolve({
          ok: true, status: 200, json: async () => ({asr: {ready: true}, tts: {ready: true}, voice_capture: {silence_ms: 900}}),
        })));
      }
      return {ok: true, status: 200, json: async () => ({}), text: async () => ""};
    },
    navigator: {
      mediaDevices: {
        getUserMedia: constraints => new Promise(resolve => granted.push({constraints, resolve})),
      },
    },
    document: {
      getElementById: id => elementFor(id),
      createElement: tag => makeElement(tag),
      addEventListener() {},
    },
  };
  sandbox.window = sandbox;
  sandbox.window.addEventListener = () => {};
  sandbox.AudioContext = class AudioContext {
    constructor() { contextsOpened += 1; this.sampleRate = 48000; }
    createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
    createScriptProcessor() { return {connect() {}, disconnect() {}, onaudioprocess: null}; }
    close() { return Promise.resolve(); }
  };
  sandbox.EventSource = class EventSource {
    constructor(url) { this.url = url; }
    addEventListener() {}
    close() {}
  };
  sandbox.CM = CM;
  vm.runInContext(source, vm.createContext(sandbox), {filename: "voice.js"});

  function makeStream(label) {
    const track = {kind: "audio", label, readyState: "live", stop() { stopped.push(label); this.readyState = "ended"; }};
    return {id: label, getTracks: () => [track], getAudioTracks: () => [track]};
  }

  // voice.js registers its call, hangup and microphone handlers without capture.
  function click(id) {
    const handlers = elementFor(id).handlers();
    for (const entry of handlers.filter(item => item.capture).concat(handlers.filter(item => !item.capture))) {
      entry.handler({type: "click", target: elementFor(id), preventDefault() {}});
    }
  }

  function releaseHealth() {
    const waiters = healthWaiters;
    healthWaiters = [];
    for (const resolve of waiters) resolve();
  }

  function releaseMic(index) {
    const entry = granted[index];
    entry.stream = makeStream("mic-" + (index + 1));
    entry.resolve(entry.stream);
  }

  function releaseAllMic() {
    for (let index = 0; index < granted.length; index += 1) {
      if (!granted[index].stream) releaseMic(index);
    }
  }

  function snapshot() {
    const voice = CM.features.voice.state;
    const mic = elementFor("voiceMicButton");
    const grantedTracks = granted
      .filter(entry => entry.stream)
      .map(entry => entry.stream.getAudioTracks()[0]);
    const adopted = voice.stream && voice.stream.getAudioTracks()[0];
    return {
      requests: granted.length,
      granted: grantedTracks.map(track => ({label: track.label, readyState: track.readyState})),
      live: grantedTracks.filter(track => track.readyState === "live").length,
      adopted: adopted ? {label: adopted.label, readyState: adopted.readyState} : null,
      stopped: stopped.slice(),
      alerts: alerts.slice(),
      contextsOpened,
      voiceActive: voice.active,
      micActive: voice.micActive,
      capturePhase: voice.capturePhase,
      status: elementFor("voiceCallStatus").textContent,
      transcript: elementFor("voiceCallTranscript").textContent,
      overlayHidden: elementFor("voiceCallOverlay").classList.contains("hidden"),
      mic: {
        disabled: mic.disabled,
        text: mic.textContent,
        active: mic.classList.contains("active"),
        muted: mic.classList.contains("muted"),
      },
    };
  }

  async function flush(turns = 8) {
    for (let i = 0; i < turns; i += 1) await new Promise(resolve => setImmediate(resolve));
  }

  return {click, releaseHealth, releaseMic, releaseAllMic, snapshot, flush};
}

// Control: a call nobody interferes with still adopts its microphone, asks for exactly one
// and hands nothing back.
async function callAlone() {
  const h = boot();
  h.click("voiceCallButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.releaseMic(0);
  await h.flush(2);
  return h.snapshot();
}

// The user hangs up while the browser still holds the microphone prompt, and only then
// answers it. The granted stream must not be adopted: the call it was asked for is over.
async function hangupDuringPrompt() {
  const h = boot();
  h.click("voiceCallButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.click("voiceHangupButton");
  await h.flush(2);
  h.releaseAllMic();
  await h.flush(2);
  return h.snapshot();
}

// The hangup lands before the runtime check answers, so the microphone prompt is never
// opened at all.
async function hangupDuringRuntimeCheck() {
  const h = boot();
  h.click("voiceCallButton");
  await h.flush(2);
  h.click("voiceHangupButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  return h.snapshot();
}

// Muting the microphone while its next prompt is already open, then starting it twice: only
// the newest start may keep a stream, the ones before it hand theirs back.
async function muteThenStartTwice() {
  const h = boot();
  h.click("voiceCallButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.releaseMic(0, "mic-1");
  await h.flush(2);
  h.click("voiceMicButton");
  await h.flush(2);
  h.click("voiceMicButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.click("voiceMicButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.releaseAllMic();
  await h.flush(2);
  return h.snapshot();
}

async function main() {
  process.stdout.write(JSON.stringify({
    callAlone: await callAlone(),
    hangupDuringPrompt: await hangupDuringPrompt(),
    hangupDuringRuntimeCheck: await hangupDuringRuntimeCheck(),
    muteThenStartTwice: await muteThenStartTwice(),
  }));
}

main().catch(error => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
"""


def _run_voice_harness(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to exercise voice.js behaviourally")
    harness = tmp_path / "voice_harness.cjs"
    harness.write_text(VOICE_HARNESS, encoding="utf-8")
    script = Path("src/character_memory/web/voice.js").resolve()
    completed = subprocess.run(
        [node, str(harness), str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_late_microphone_permission_cannot_leave_the_call_microphone_on(tmp_path):
    payload = _run_voice_harness(tmp_path)

    # Control: an uninterrupted call adopts its microphone, keeps exactly one track live and
    # opens no second prompt.
    alone = payload["callAlone"]
    assert alone["requests"] == 1
    assert alone["stopped"] == []
    assert alone["live"] == 1
    assert alone["contextsOpened"] == 1
    assert alone["voiceActive"] is True
    assert alone["micActive"] is True
    assert alone["adopted"] == {"label": "mic-1", "readyState": "live"}
    assert alone["mic"] == {"disabled": False, "text": "🎙 麦克风", "active": True, "muted": False}

    # The call ends while the prompt is still open: the late "Allow" is real hardware that no
    # call owns any more, so it is handed straight back instead of going live behind a closed
    # overlay with every microphone control disabled.
    hung_up = payload["hangupDuringPrompt"]
    assert hung_up["granted"][0]["readyState"] == "ended", "the microphone answered after the hangup was left live"
    assert hung_up["stopped"] == ["mic-1"]
    assert hung_up["live"] == 0
    assert hung_up["adopted"] is None
    assert hung_up["contextsOpened"] == 0, "a released stream still got an AudioContext"
    assert hung_up["voiceActive"] is False
    assert hung_up["micActive"] is False
    assert hung_up["capturePhase"] == "idle"
    assert hung_up["overlayHidden"] is True
    assert hung_up["mic"]["disabled"] is True, "the page kept a control that claims to hold a live microphone"
    assert hung_up["status"] != "正在听…", "a retired start wrote its listening status back onto the ended call"

    # The hangup lands during the runtime check: no microphone prompt is opened afterwards.
    during_check = payload["hangupDuringRuntimeCheck"]
    assert during_check["requests"] == 0, "a retired call still opened a microphone prompt"
    assert during_check["stopped"] == []
    assert during_check["voiceActive"] is False
    assert during_check["micActive"] is False
    assert during_check["contextsOpened"] == 0

    # A newer start retires the one before it, whichever answer the browser lands first: after
    # two parked prompts exactly one stream stays live and the other two were handed back.
    twice = payload["muteThenStartTwice"]
    assert twice["requests"] == 3
    assert twice["live"] == 1, "two microphone streams are live at once"
    assert twice["stopped"] == ["mic-1", "mic-2"]
    assert twice["adopted"] == {"label": "mic-3", "readyState": "live"}
    # The call's own start plus the newest retry: the retired start handed its track back
    # before an AudioContext was ever built for it.
    assert twice["contextsOpened"] == 2
    assert twice["voiceActive"] is True
    assert twice["micActive"] is True
    assert twice["alerts"] == []
