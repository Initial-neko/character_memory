import json
from pathlib import Path
import shutil
import subprocess

import pytest


VOICE_TTS_FAILURE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const sharedAudioSource = fs.readFileSync(process.argv[3], "utf8");
const scenario = process.argv[4] || "single";

const sources = [];
const chatCalls = [];
const revoked = [];
const delayedTimers = [];
const silent = () => {};

function element() {
  return {
    classList:{add:silent, remove:silent, toggle:silent},
    textContent:"", innerHTML:"", title:"", disabled:false,
    scrollTop:0, scrollHeight:0,
    addEventListener:silent,
    setAttribute:silent,
    appendChild:silent,
    append:silent,
    querySelector:() => null,
  };
}
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  createElement:() => element(),
};

class EventSource {
  constructor(url) { this.url = url; this.handlers = {}; sources.push(this); }
  addEventListener(name, handler) { (this.handlers[name] ||= []).push(handler); }
  close() {}
  emit(name, data) {
    for (const handler of this.handlers[name] || []) handler({data:JSON.stringify(data || {})});
  }
}

const CM = {
  state:{characters:[{id:"haru", name:"Haru"}], characterId:"haru", conversation:{}},
  dom:{},
  features:{},
  on:silent,
  registerFeature(name, api) { this.features[name] = api; },
  isGroupConversation:() => false,
  currentProfile:() => ({id:"haru", name:"Haru"}),
  conversationIdFor:id => `direct:${id}`,
  directEventToMessage:value => value,
  mergeDirectMessage:silent,
};

async function fetchStub(url, options = {}) {
  const target = String(url);
  if (target.endsWith("/health")) {
    return {ok:true, status:200, json:async () => ({asr:{ready:true}, tts:{ready:true}}), text:async () => ""};
  }
  if (target.includes("/v1/tts")) {
    return {ok:false, status:503, text:async () => "provider unavailable"};
  }
  if (target === "/v1/chat/messages") {
    chatCalls.push(JSON.parse(options.body));
    return {ok:true, status:202, json:async () => ({message:{id:99}}), text:async () => ""};
  }
  return {ok:true, status:200, json:async () => ({}), text:async () => ""};
}

const sandbox = {
  window:{
    CM,
    VisualCapture:{createSession:() => ({
      getState:() => ({active:false, source:null, candidateCount:0}),
      startCamera:async () => {}, startDisplay:async () => {}, stop:silent,
    })},
    AudioContext:class {
      constructor() { this.sampleRate = 48000; this.destination = {}; }
      createMediaStreamSource() { return {connect:silent, disconnect:silent}; }
      createScriptProcessor() { return {connect:silent, disconnect:silent, onaudioprocess:null}; }
      close() { return Promise.resolve(); }
    },
    addEventListener:silent,
  },
  document,
  navigator:{mediaDevices:{getUserMedia:async () => ({getTracks:() => []})}},
  localStorage:{getItem:() => null},
  fetch:fetchStub,
  EventSource,
  Audio:class {},
  URL:{createObjectURL:() => "blob:unused", revokeObjectURL:url => revoked.push(url)},
  URLSearchParams,
  performance:{now:() => Date.now()},
  alert:silent,
  console:{debug:silent, warn:silent, log:silent, error:silent, info:silent},
  setTimeout:(fn, delay) => { if (delay === 1200) delayedTimers.push(fn); else Promise.resolve().then(fn); return 1; },
  clearTimeout:silent,
  setInterval:() => 1,
  clearInterval:silent,
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.navigator = sandbox.navigator;
sandbox.window.localStorage = sandbox.localStorage;
sandbox.window.fetch = fetchStub;
sandbox.window.EventSource = EventSource;
sandbox.window.Audio = sandbox.Audio;
sandbox.window.URL = sandbox.URL;
sandbox.window.URLSearchParams = URLSearchParams;
sandbox.window.performance = sandbox.performance;
sandbox.window.alert = sandbox.alert;
sandbox.window.console = sandbox.console;
sandbox.window.setTimeout = sandbox.setTimeout;
sandbox.window.clearTimeout = sandbox.clearTimeout;
sandbox.window.setInterval = sandbox.setInterval;
sandbox.window.clearInterval = sandbox.clearInterval;

const context = vm.createContext(sandbox);
vm.runInContext(sharedAudioSource, context, {filename:"media_audio.js"});
vm.runInContext(source, context, {filename:"voice.js"});

async function main() {
  const feature = CM.features.voice;
  await feature.start();
  feature.state.pendingTurns.push({text:"别漏掉我刚才说的", visualFrames:[], asrMs:12});
  sources[0].emit("character_event", {
    id:1, character_id:"haru", content:"上一轮回答", metadata:{action:"MESSAGE"},
  });
  sources[0].emit("character_event", {
    id:2, character_id:"haru", content:"同一轮排队的第二段回答", metadata:{action:"MESSAGE"},
  });
  for (let attempt = 0; attempt < 100 && delayedTimers.length === 0; attempt += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
  if (!delayedTimers.length) throw new Error("expected TTS failure recovery timer");
  if (scenario === "overlap") {
    // A second recognized utterance during error recovery must join the first
    // pending turn, not overtake it with a separate simultaneous request.
    feature.state.pendingTurns.push({text:"第二句话", visualFrames:[], asrMs:8});
    if (!feature.state.recoveryPending) throw new Error("recovery gate already released");
  }
  delayedTimers.shift()();
  for (let attempt = 0; attempt < 100 && chatCalls.length === 0; attempt += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
  process.stdout.write(JSON.stringify({
    chatCalls,
    pendingTurns:feature.state.pendingTurns.length,
    queueLength:feature.state.queue.length,
    phase:feature.state.phase,
    revoked,
    recoveryPending:feature.state.recoveryPending,
  }));
}
main().catch(error => { process.stderr.write(String(error.stack || error)); process.exit(1); });
"""


def test_tts_failure_releases_the_reply_queue_and_flushes_heard_user_turn(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to exercise voice.js behaviorally")

    harness = tmp_path / "voice_tts_failure.cjs"
    harness.write_text(VOICE_TTS_FAILURE_HARNESS, encoding="utf-8")
    script = Path("src/character_memory/web/voice.js").resolve()
    completed = subprocess.run(
        [node, str(harness), str(script), str(script.with_name("media_audio.js")), "single"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)

    assert payload["queueLength"] == 0
    assert payload["pendingTurns"] == 0
    assert payload["chatCalls"] == [
        {
            "message": "别漏掉我刚才说的",
            "character_id": "haru",
            "conversation_id": "direct:haru",
        }
    ]


def test_tts_failure_recovery_merges_speech_heard_during_timeout(tmp_path):
    """Recovery keeps the dispatch gate closed until pending turns are flushed."""

    node = shutil.which("node")
    assert node, "Node is required for the production voice/media-audio contract"
    harness = tmp_path / "voice_tts_overlap.cjs"
    harness.write_text(VOICE_TTS_FAILURE_HARNESS, encoding="utf-8")
    script = Path("src/character_memory/web/voice.js").resolve()
    completed = subprocess.run(
        [node, str(harness), str(script), str(script.with_name("media_audio.js")), "overlap"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["chatCalls"] == [
        {
            "message": "别漏掉我刚才说的\\n第二句话",
            "character_id": "haru",
            "conversation_id": "direct:haru",
        }
    ]
    assert payload["pendingTurns"] == 0
    assert payload["recoveryPending"] is False
