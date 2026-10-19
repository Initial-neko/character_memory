from pathlib import Path


def test_voice_call_prefers_streaming_asr_and_keeps_batch_fallback():
    source = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")

    assert "health?.asr?.streaming" in source
    assert "window.StreamingAsr?.createSession" in source
    assert 'source: "call"' in source
    assert "await session.connect();" in source
    assert "await session.attach(context, source);" in source

    # Compatibility remains first-class for older/non-streaming runtimes and
    # becomes the in-place recovery path if a live WebSocket dies.
    assert "createScriptProcessor(2048, 1, 1)" in source
    assert "/v1/asr" in source
    assert '"X-ASR-Source":"call"' in source
    assert "streaming ASR failed; switching to batch capture" in source


def test_streaming_call_final_preserves_interrupt_queue_and_visual_segment_window():
    source = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")

    assert "function handleStreamingPartial(data)" in source
    assert "function handleStreamingFinal(data)" in source
    assert "voice.streamingSegmentId = key;" in source
    assert "voice.streamingSegmentStartedAt = performance.now();" in source
    assert "voice.streamingSegmentId === key && voice.streamingSegmentStartedAt" in source
    assert "fromMs:Math.max(0, startedAt - 1000)" in source

    # A recognized user turn while the character is thinking/speaking must be
    # queued exactly like the old batch path, not dropped or dispatched into an
    # already-running reaction.
    final_start = source.index("async function handleStreamingFinal(data)")
    final_end = source.index("async function attachStreamingAsr", final_start)
    final_body = source[final_start:final_end]
    assert "if (replyInFlight())" in final_body
    assert "voice.pendingTurns.push(turn)" in final_body
    assert "await dispatchRecognizedTurn(turn)" in final_body

    # Streaming does not fabricate a fake zero-latency ASR metric.
    assert "asrMs:null" in final_body


def test_stopping_call_cancels_streaming_session_and_retires_pending_mic_owner():
    source = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")

    stop_start = source.index("async function stopMicrophone")
    stop_end = source.index("async function startMicrophone", stop_start)
    stop_body = source[stop_start:stop_end]

    assert "micOwnerSeq = claimMicTicket()" in stop_body
    assert "voice.asrSession?.cancel?.()" in stop_body
    assert "voice.streamingAsr = false" in stop_body
    assert "mediaAudio.closeCapture" in stop_body
    assert "stream:voice.stream" in stop_body
    assert "context:voice.audioContext" in stop_body


STREAMING_WATCHDOG_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const sharedAudioSource = fs.readFileSync(process.argv[3], "utf8");
const silent = () => {};
const timers = [];
let asrCallbacks = null;

function element() {
  return {
    dataset:{}, classList:{add:silent, remove:silent, toggle:silent},
    textContent:"", innerHTML:"", title:"", disabled:false,
    scrollTop:0, scrollHeight:0,
    addEventListener:silent, setAttribute:silent,
    appendChild:silent, append:silent, querySelector:() => null,
  };
}
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  querySelector:() => document.getElementById("call-card"),
  createElement:() => element(),
};

class EventSource {
  constructor(url) { this.url = url; this.handlers = {}; }
  addEventListener(name, handler) { (this.handlers[name] ||= []).push(handler); }
  close() {}
}
class FakeWebSocket { static OPEN = 1; }

const CM = {
  state:{characters:[{id:"haru", name:"Haru"}], characterId:"haru", conversation:{}},
  dom:{}, features:{}, on:silent,
  registerFeature(name, api) { this.features[name] = api; },
  isGroupConversation:() => false,
  currentProfile:() => ({id:"haru", name:"Haru"}),
  conversationIdFor:id => `direct:${id}`,
  directEventToMessage:value => value,
  mergeDirectMessage:silent,
};

async function fetchStub(url) {
  const target = String(url);
  if (target.endsWith("/health")) {
    return {ok:true, status:200, json:async () => ({
      asr:{ready:true, streaming:true, silence_ms:900},
      tts:{ready:true},
    }), text:async () => ""};
  }
  if (target === "/v1/chat/messages") {
    return {ok:true, status:202, json:async () => ({message:{id:99}}), text:async () => ""};
  }
  return {ok:true, status:200, json:async () => ({}), text:async () => ""};
}

function fakeSetTimeout(fn, delay) {
  const timer = {fn, delay, canceled:false};
  timers.push(timer);
  return timer;
}
function fakeClearTimeout(timer) { if (timer && typeof timer === "object") timer.canceled = true; }

const sandbox = {
  window:{
    CM,
    WebSocket:FakeWebSocket,
    VisualCapture:{createSession:() => ({
      getState:() => ({active:false, source:null, candidateCount:0}),
      startCamera:async () => {}, startDisplay:async () => {}, stop:silent,
    })},
    StreamingAsr:{createSession:options => {
      asrCallbacks = options;
      return {
        connect:async () => {}, attach:async () => {}, close:silent, cancel:silent,
        getState:() => ({ready:true, closed:false}),
      };
    }},
    AudioContext:class {
      constructor() {
        this.sampleRate = 48000; this.destination = {};
        this.audioWorklet = {addModule:async () => {}};
      }
      createMediaStreamSource() { return {connect:silent, disconnect:silent}; }
      createScriptProcessor() { return {connect:silent, disconnect:silent, onaudioprocess:null}; }
      close() { return Promise.resolve(); }
    },
    AudioWorkletNode:class {
      constructor() { this.port = {onmessage:null}; }
      connect() {}
      disconnect() {}
    },
    addEventListener:silent,
  },
  document,
  navigator:{mediaDevices:{getUserMedia:async () => ({getTracks: () => []})}},
  localStorage:{getItem:() => null},
  fetch:fetchStub,
  EventSource,
  Audio:class {},
  URL:{createObjectURL:() => "blob:unused", revokeObjectURL:silent},
  URLSearchParams,
  performance:{now:() => Date.now()},
  alert:silent,
  console:{debug:silent, warn:silent, log:silent, error:silent, info:silent},
  setTimeout:fakeSetTimeout,
  clearTimeout:fakeClearTimeout,
  setInterval:() => 1,
  clearInterval:silent,
};
sandbox.window.window = sandbox.window;
for (const key of ["document","navigator","localStorage","fetch","EventSource","Audio","URL","URLSearchParams","performance","alert","console","setTimeout","clearTimeout","setInterval","clearInterval"]) {
  sandbox.window[key] = sandbox[key];
}
sandbox.window.WebSocket = FakeWebSocket;

const context = vm.createContext(sandbox);
vm.runInContext(sharedAudioSource, context, {filename:"media_audio.js"});
vm.runInContext(source, context, {filename:"voice.js"});
vm.runInContext(fs.readFileSync(require("path").join(require("path").dirname(process.argv[2]), "call_stage.js"), "utf8"), context, {filename:"call_stage.js"});

(async () => {
  const feature = CM.features.voice;
  await feature.start();
  const started = await feature.startMicrophone();
  if (!started) throw new Error("microphone did not start");
  if (!asrCallbacks) throw new Error("streaming session was not created");

  asrCallbacks.onPartial({session_id:"s", segment_id:1, text:"你好"});
  if (feature.state.capturePhase !== "recording") throw new Error("partial did not enter recording");
  const watchdog = timers.find(timer => timer.delay === 8000 && !timer.canceled);
  if (!watchdog) throw new Error("partial did not arm a segment watchdog");

  watchdog.fn();
  const afterTimeout = {
    phase:feature.state.capturePhase,
    segment:feature.state.streamingSegmentId,
    transcript:document.getElementById("voiceCallTranscript").textContent,
  };
  if (afterTimeout.phase !== "listening" || afterTimeout.segment !== null)
    throw new Error("watchdog did not return to listening: " + JSON.stringify(afterTimeout));

  asrCallbacks.onFinal({session_id:"s", segment_id:1, text:"迟到的完整结果"});
  await new Promise(resolve => setImmediate(resolve));
  if (feature.state.pendingTurns.length !== 0)
    throw new Error("late final was not discarded");
  if (feature.state.seenAsrSegments.has("s:1") !== true)
    throw new Error("timed-out segment was not retired");

  // A normal final must cancel the watchdog for its own segment.
  asrCallbacks.onPartial({session_id:"s", segment_id:2, text:"下一句"});
  const second = [...timers].reverse().find(timer => timer.delay === 8000 && !timer.canceled);
  if (!second) throw new Error("second partial did not arm a watchdog");
  asrCallbacks.onFinal({session_id:"s", segment_id:2, text:"下一句完整"});
  await new Promise(resolve => setImmediate(resolve));
  if (!second.canceled) throw new Error("final did not cancel the segment watchdog");

  process.stdout.write(JSON.stringify({afterTimeout, pending:feature.state.pendingTurns.length}));
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exit(1); });
"""


def test_streaming_partial_without_final_returns_to_listening_and_drops_late_final(tmp_path):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to exercise voice.js behaviorally")

    harness = tmp_path / "voice_stream_watchdog.cjs"
    harness.write_text(STREAMING_WATCHDOG_HARNESS, encoding="utf-8")
    script = Path("src/character_memory/web/voice.js").resolve()
    completed = subprocess.run(
        [node, str(harness), str(script), str(script.with_name("media_audio.js"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["afterTimeout"]["phase"] == "listening"
    assert payload["afterTimeout"]["segment"] is None
    assert "回到聆听" in payload["afterTimeout"]["transcript"]
    assert payload["pending"] == 0
