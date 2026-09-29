import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_browser_loads_shared_stream_client_before_voice_features():
    web = Path("src/character_memory/web")
    index = (web / "index.html").read_text(encoding="utf-8")
    client = (web / "asr_stream.js").read_text(encoding="utf-8")
    worklet = (web / "asr_pcm_worklet.js").read_text(encoding="utf-8")

    assert "/static/asr_stream.js" in index
    assert index.index("/static/asr_stream.js") < index.index("/static/voice.js")
    assert index.index("/static/asr_stream.js") < index.index("/static/dictation.js")

    assert "/v1/asr/stream" in client
    assert "AudioWorkletNode" in client
    assert 'processorOptions: {targetSampleRate:16000}' in client
    assert 'command("flush"' in client
    assert 'command("cancel")' in client
    assert 'data.kind === "partial"' in client
    assert 'data.kind === "final"' in client
    assert "createScriptProcessor" not in client

    assert 'registerProcessor("character-memory-asr-capture"' in worklet
    assert "Int16Array.from(values)" in worklet


WORKLET_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const frames = [];
let Processor = null;

class AudioWorkletProcessor {
  constructor() {
    this.port = {
      postMessage(buffer) {
        frames.push(new Int16Array(buffer));
      },
    };
  }
}

const sandbox = {
  AudioWorkletProcessor,
  sampleRate: 48000,
  registerProcessor(name, cls) {
    if (name !== "character-memory-asr-capture") throw new Error("wrong processor name");
    Processor = cls;
  },
  Math,
  Number,
  Int16Array,
};
vm.runInContext(source, vm.createContext(sandbox), {filename:"asr_pcm_worklet.js"});
if (!Processor) throw new Error("processor was not registered");

const processor = new Processor({processorOptions:{targetSampleRate:16000}});
const input = new Float32Array(480);
input.fill(0.5);
const output = new Float32Array(128);
processor.process([[input]], [[output]]);

const flattened = Array.from(frames).flatMap(frame => Array.from(frame));
process.stdout.write(JSON.stringify({
  samples: flattened.length,
  first: flattened[0],
  outputSilent: Array.from(output).every(value => value === 0),
}));
"""


def test_audio_worklet_emits_16khz_pcm16_without_audible_output(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to exercise the AudioWorklet processor")

    harness = tmp_path / "asr_worklet_harness.cjs"
    harness.write_text(WORKLET_HARNESS, encoding="utf-8")
    script = Path("src/character_memory/web/asr_pcm_worklet.js").resolve()
    completed = subprocess.run(
        [node, str(harness), str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    # 480 input samples at 48 kHz represent 10 ms; 16 kHz output must therefore
    # contain exactly 160 samples. A constant 0.5 signal should map near 16384.
    assert result["samples"] == 160
    assert 16380 <= result["first"] <= 16384
    assert result["outputSilent"] is True
