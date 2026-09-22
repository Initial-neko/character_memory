from pathlib import Path
import json
import shutil
import subprocess

import pytest


def test_chat_composer_exposes_asr_dictation_button():
    html = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    assert 'id="voiceInputButton"' in html
    assert '/static/dictation.js' in html
    assert '语音转文字' in html


def test_dictation_uses_media_asr_and_does_not_auto_send_or_tts():
    script = Path("src/character_memory/web/dictation.js").read_text(encoding="utf-8")
    assert '/v1/asr' in script
    assert '/v1/tts' not in script
    assert '/v1/chat/messages' not in script
    assert 'insertTranscript(text)' in script
    assert 'CM.dom.input' in script
    assert 'CM.isGroupConversation()' not in script


def test_dictation_and_call_do_not_capture_microphone_together():
    script = Path("src/character_memory/web/dictation.js").read_text(encoding="utf-8")
    assert 'CM.features.voice?.state?.active' in script
    assert 'voiceCallButton' in script
    assert 'recognize:false' in script


def test_dictation_script_is_valid_javascript_when_node_is_available():
    node = shutil.which("node")
    if not node:
        return
    path = Path("src/character_memory/web/dictation.js")
    checked = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


IDLE_BUTTON = {"disabled": False, "text": "🎤", "recording": False, "transcribing": False}


# dictation.js drives a real permission prompt, which is the only thing that makes the
# interleavings reachable: the browser answers whenever the user gets around to it, and by
# then the user may have asked for a call instead. The harness parks /health and
# getUserMedia on promises the scenario releases by hand, so the "prompt is still open"
# window can be held open on command, and voice.js side of a call is reduced to what
# dictation can observe: it checks the runtime, asks for the microphone, and only promotes
# the call to active once the browser hands the stream over (voice.js:749).
DICTATION_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");

let granted = [];
let stopped = [];
let alerts = [];
let asrCalls = [];
let healthWaiters = [];
let elements = new Map();

function makeElement(id) {
  const classes = new Set();
  const attributes = {};
  const handlers = [];
  return {
    id,
    disabled: false,
    textContent: "",
    title: "",
    value: "",
    selectionStart: 0,
    selectionEnd: 0,
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
    focus() {},
    setSelectionRange() {},
    dispatchEvent() { return true; },
  };
}

function boot() {
  granted = [];
  stopped = [];
  alerts = [];
  asrCalls = [];
  healthWaiters = [];
  elements = new Map();

  function elementFor(id) {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  }

  const CM = {
    dom: {input: elementFor("messageInput")},
    features: {voice: {state: {active: false}}},
    events: {},
    on(name, handler) { CM.events[name] = handler; },
    registerFeature(name, feature) { CM.features[name] = feature; return feature; },
  };

  const sandbox = {
    console: {debug() {}, log() {}, warn() {}, error() {}, info() {}},
    localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
    alert: message => alerts.push(message),
    Event: class Event { constructor(type, init) { this.type = type; Object.assign(this, init || {}); } },
    fetch: async url => {
      if (String(url).endsWith("/health")) {
        return new Promise(resolve => healthWaiters.push(() => resolve({
          ok: true, status: 200, json: async () => ({asr: {ready: true}}), text: async () => "",
        })));
      }
      asrCalls.push(String(url));
      return {ok: true, status: 200, json: async () => ({text: "识别结果"}), text: async () => ""};
    },
    navigator: {
      mediaDevices: {
        getUserMedia: constraints => new Promise(resolve => granted.push({
          constraints,
          stack: (new Error().stack || ""),
          resolve,
        })),
      },
    },
    document: {getElementById: id => elementFor(id), addEventListener() {}},
  };
  sandbox.window = sandbox;
  sandbox.window.addEventListener = () => {};
  sandbox.AudioContext = class AudioContext {
    constructor() { this.sampleRate = 48000; }
    createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
    createScriptProcessor() { return {connect() {}, disconnect() {}, onaudioprocess: null}; }
    close() { return Promise.resolve(); }
  };
  sandbox.CM = CM;
  vm.runInContext(source, vm.createContext(sandbox), {filename: "dictation.js"});

  const mediaDevices = sandbox.navigator.mediaDevices;

  function makeStream(label) {
    const track = {kind: "audio", label, readyState: "live", stop() { stopped.push(label); this.readyState = "ended"; }};
    return {id: label, getTracks: () => [track], getAudioTracks: () => [track]};
  }

  // On the target element itself the dispatch order is capture listeners first, then
  // bubble listeners -- the order the real page relies on, because dictation.js registers
  // its call-button listener with capture and voice.js registers its startCall without.
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

  function releaseMic(index, label) {
    const entry = granted[index];
    entry.stream = makeStream(label);
    entry.resolve(entry.stream);
  }

  function requester(entry) {
    return entry.stack.includes("dictation.js") ? "dictation" : "call";
  }

  function snapshot() {
    const button = elementFor("voiceInputButton");
    return {
      requests: granted.map(entry => requester(entry)),
      mics: granted.map(entry => {
        const track = entry.stream && entry.stream.getAudioTracks()[0];
        return {requester: requester(entry), label: track && track.label, readyState: track && track.readyState};
      }),
      recording: CM.features.dictation.state.recording,
      busy: CM.features.dictation.state.busy,
      voiceActive: CM.features.voice.state.active,
      stopped: stopped.slice(),
      alerts: alerts.slice(),
      asrCalls: asrCalls.slice(),
      button: {
        disabled: button.disabled,
        text: button.textContent,
        recording: button.classList.contains("recording"),
        transcribing: button.classList.contains("transcribing"),
      },
    };
  }

  async function flush(turns = 8) {
    for (let i = 0; i < turns; i += 1) await new Promise(resolve => setImmediate(resolve));
  }

  async function startCall() {
    await sandbox.fetch("http://127.0.0.1:8001/health");
    const stream = await mediaDevices.getUserMedia({audio: true});
    CM.features.voice.state.active = true;
    return stream;
  }

  // voice.js hangs startCall off the same button as a bubble listener, so a click on
  // 📞 reaches dictation's capture listener first and this one second.
  elementFor("voiceCallButton").addEventListener("click", () => { startCall(); });

  function switchConversation() {
    CM.events.conversationChanged({type: "DIRECT", characterId: "someone-else"});
  }

  return {click, releaseHealth, releaseMic, snapshot, flush, switchConversation};
}

async function dictationAlone() {
  const h = boot();
  h.click("voiceInputButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.releaseMic(0, "dictation-mic");
  await h.flush(2);
  return h.snapshot();
}

async function callThenDictation() {
  const h = boot();
  h.click("voiceCallButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.click("voiceInputButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.releaseMic(0, "call-mic");
  await h.flush(2);
  h.releaseMic(1, "dictation-mic");
  await h.flush(2);
  return h.snapshot();
}

async function dictationThenCall() {
  const h = boot();
  h.click("voiceInputButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.click("voiceCallButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.releaseMic(0, "dictation-mic");
  await h.flush(2);
  h.releaseMic(1, "call-mic");
  await h.flush(2);
  return h.snapshot();
}

async function callClickDuringRuntimeCheck() {
  const h = boot();
  h.click("voiceInputButton");
  await h.flush(2);
  h.click("voiceCallButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  return h.snapshot();
}

async function callDuringRecording() {
  const h = boot();
  h.click("voiceInputButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.releaseMic(0, "dictation-mic");
  await h.flush(2);
  h.click("voiceCallButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.releaseMic(1, "call-mic");
  await h.flush(2);
  return h.snapshot();
}

async function switchDuringPrompt() {
  const h = boot();
  h.click("voiceInputButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.switchConversation();
  await h.flush(2);
  h.releaseMic(0, "dictation-mic");
  await h.flush(2);
  return h.snapshot();
}

async function switchDuringRecording() {
  const h = boot();
  h.click("voiceInputButton");
  await h.flush(2);
  h.releaseHealth();
  await h.flush(2);
  h.releaseMic(0, "dictation-mic");
  await h.flush(2);
  h.switchConversation();
  await h.flush(2);
  return h.snapshot();
}

async function main() {
  process.stdout.write(JSON.stringify({
    dictationAlone: await dictationAlone(),
    callThenDictation: await callThenDictation(),
    dictationThenCall: await dictationThenCall(),
    callClickDuringRuntimeCheck: await callClickDuringRuntimeCheck(),
    callDuringRecording: await callDuringRecording(),
    switchDuringPrompt: await switchDuringPrompt(),
    switchDuringRecording: await switchDuringRecording(),
  }));
}

main().catch(error => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
"""


def test_late_permission_answer_cannot_leave_a_second_microphone_on(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to exercise dictation.js behaviourally")

    harness = tmp_path / "dictation_harness.cjs"
    harness.write_text(DICTATION_HARNESS, encoding="utf-8")
    script = Path("src/character_memory/web/dictation.js").resolve()
    completed = subprocess.run(
        [node, str(harness), str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)

    # Control: a start nobody interferes with still adopts the stream it was granted,
    # asks for exactly one microphone and hands nothing back.
    alone = payload["dictationAlone"]
    assert alone["requests"] == ["dictation"]
    assert alone["stopped"] == []
    assert alone["recording"] is True
    assert alone["voiceActive"] is False
    assert alone["mics"] == [{"requester": "dictation", "label": "dictation-mic", "readyState": "live"}]
    assert alone["button"] == {"disabled": False, "text": "■", "recording": True, "transcribing": False}

    # Call first, then dictation, answered in request order: the call owns the microphone,
    # so the dictation start that was waiting on the same prompt has to give its stream back.
    call_first = payload["callThenDictation"]
    assert call_first["requests"] == ["call", "dictation"]
    assert call_first["stopped"] == ["dictation-mic"], "the microphone the call already owns was left live"
    assert [mic["readyState"] for mic in call_first["mics"]] == ["live", "ended"], "two microphones are live at once"
    assert call_first["recording"] is False
    assert call_first["voiceActive"] is True
    assert call_first["button"] == IDLE_BUTTON, "the button still claims a recording that never started"

    # Dictation first, then call: the click that starts the call retires the start, even
    # though the browser answers dictation's prompt before the call's.
    dictation_first = payload["dictationThenCall"]
    assert dictation_first["requests"] == ["dictation", "call"]
    assert dictation_first["stopped"] == ["dictation-mic"], "the microphone the call already owns was left live"
    assert [mic["readyState"] for mic in dictation_first["mics"]] == ["ended", "live"]
    assert dictation_first["recording"] is False
    assert dictation_first["voiceActive"] is True
    assert dictation_first["button"] == IDLE_BUTTON

    # The call click lands while the runtime is still being checked, so the microphone
    # prompt is never opened at all -- only the call may ask for one.
    during_check = payload["callClickDuringRuntimeCheck"]
    assert during_check["requests"] == ["call"], "a retired start still opened a microphone prompt"
    assert during_check["stopped"] == []
    assert during_check["recording"] is False
    assert during_check["button"] == IDLE_BUTTON

    # Unchanged path: a call clicked while dictation is already recording stops the
    # capture without sending the audio to ASR, and the call keeps its own stream.
    while_recording = payload["callDuringRecording"]
    assert while_recording["stopped"] == ["dictation-mic"]
    assert [mic["readyState"] for mic in while_recording["mics"]] == ["ended", "live"]
    assert while_recording["recording"] is False
    assert while_recording["voiceActive"] is True
    assert while_recording["asrCalls"] == []
    assert while_recording["busy"] is False
    assert while_recording["button"] == IDLE_BUTTON

    # Switching conversation retires the start too: the capture was asked for in the
    # conversation the user just left, so the late "Allow" is released, not adopted, and
    # nothing is sent to ASR for the conversation that is now on screen.
    switched_while_pending = payload["switchDuringPrompt"]
    assert switched_while_pending["requests"] == ["dictation"]
    assert switched_while_pending["stopped"] == ["dictation-mic"], "switching conversation left the microphone on"
    assert switched_while_pending["recording"] is False
    assert switched_while_pending["asrCalls"] == []
    assert switched_while_pending["button"] == IDLE_BUTTON

    # Unchanged path: a recording that is already live is dropped on the same event,
    # without a transcript.
    switched_while_recording = payload["switchDuringRecording"]
    assert switched_while_recording["stopped"] == ["dictation-mic"]
    assert switched_while_recording["recording"] is False
    assert switched_while_recording["asrCalls"] == []
    assert switched_while_recording["busy"] is False
    assert switched_while_recording["button"] == IDLE_BUTTON

