(() => {
  const CM = window.CM;
  if (!CM) return;

  const MEDIA_BASE_KEY = "character-memory:media-base-url";
  const mediaBase = () => localStorage.getItem(MEDIA_BASE_KEY) || "http://127.0.0.1:8001";
  const button = document.getElementById("voiceInputButton");
  const state = {
    recording: false,
    busy: false,
    stream: null,
    audioContext: null,
    sourceNode: null,
    processor: null,
    chunks: [],
  };

  function setButton(mode) {
    if (!button) return;
    button.classList.toggle("recording", mode === "recording");
    button.classList.toggle("transcribing", mode === "transcribing");
    button.disabled = mode === "starting" || mode === "transcribing";
    if (mode === "recording") {
      button.textContent = "■";
      button.title = "停止录音并识别";
      button.setAttribute("aria-label", "停止录音并识别");
      return;
    }
    if (mode === "transcribing") {
      button.textContent = "…";
      button.title = "正在识别语音";
      button.setAttribute("aria-label", "正在识别语音");
      return;
    }
    button.textContent = "🎤";
    button.title = "语音输入（识别后放入输入框）";
    button.setAttribute("aria-label", "语音输入");
  }

  async function checkAsr() {
    const response = await fetch(`${mediaBase()}/health`);
    if (!response.ok) throw new Error(`Media Runtime ${response.status}`);
    const health = await response.json();
    if (!health.asr?.ready) throw new Error(health.asr?.reason || "ASR 未配置");
    return health;
  }

  function concatChunks(chunks) {
    const length = chunks.reduce((sum, item) => sum + item.length, 0);
    const result = new Float32Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      result.set(chunk, offset);
      offset += chunk.length;
    }
    return result;
  }

  function downsample(samples, sourceRate, targetRate = 16000) {
    if (sourceRate === targetRate) return samples;
    const ratio = sourceRate / targetRate;
    const outLength = Math.max(1, Math.round(samples.length / ratio));
    const out = new Float32Array(outLength);
    for (let i = 0; i < outLength; i += 1) {
      const pos = i * ratio;
      const left = Math.floor(pos);
      const right = Math.min(samples.length - 1, left + 1);
      const frac = pos - left;
      out[i] = samples[left] * (1 - frac) + samples[right] * frac;
    }
    return out;
  }

  function wavBlob(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const writeString = (offset, value) => {
      for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
    };
    writeString(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(36, "data");
    view.setUint32(40, samples.length * 2, true);
    let offset = 44;
    for (let i = 0; i < samples.length; i += 1) {
      const value = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(offset, value < 0 ? value * 32768 : value * 32767, true);
      offset += 2;
    }
    return new Blob([buffer], {type:"audio/wav"});
  }

  function cleanupCapture() {
    state.processor?.disconnect?.();
    state.sourceNode?.disconnect?.();
    state.stream?.getTracks?.().forEach(track => track.stop());
    const context = state.audioContext;
    state.stream = null;
    state.audioContext = null;
    state.sourceNode = null;
    state.processor = null;
    state.recording = false;
    if (context) context.close().catch(() => {});
  }

  function insertTranscript(text) {
    const input = CM.dom.input;
    if (!input) return;
    const value = String(text || "").trim();
    if (!value) return;
    const start = Number.isInteger(input.selectionStart) ? input.selectionStart : input.value.length;
    const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : start;
    const before = input.value.slice(0, start);
    const after = input.value.slice(end);
    const beforeSpace = /[A-Za-z0-9]$/.test(before) && /^[A-Za-z0-9]/.test(value) ? " " : "";
    const afterSpace = /[A-Za-z0-9]$/.test(value) && /^[A-Za-z0-9]/.test(after) ? " " : "";
    const inserted = `${beforeSpace}${value}${afterSpace}`;
    input.value = `${before}${inserted}${after}`;
    const cursor = before.length + inserted.length;
    input.setSelectionRange(cursor, cursor);
    input.dispatchEvent(new Event("input", {bubbles:true}));
    input.focus();
  }

  async function transcribe(chunks, sourceRate) {
    if (!chunks.length) return;
    state.busy = true;
    setButton("transcribing");
    try {
      const raw = concatChunks(chunks);
      if (raw.length < Math.max(1, Math.floor(sourceRate * 0.15))) throw new Error("录音太短");
      const pcm = downsample(raw, sourceRate, 16000);
      const response = await fetch(`${mediaBase()}/v1/asr`, {
        method: "POST",
        headers: {"Content-Type":"audio/wav"},
        body: wavBlob(pcm, 16000),
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      const text = String(result.text || "").trim();
      if (!text) throw new Error("没有识别到文字");
      insertTranscript(text);
    } catch (error) {
      alert(`语音识别失败：${error.message}`);
    } finally {
      state.busy = false;
      setButton("idle");
    }
  }

  function audioFrame(event) {
    if (!state.recording) return;
    state.chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
  }

  async function startRecording() {
    if (state.recording || state.busy) return;
    if (CM.features.voice?.state?.active) {
      alert("请先结束语音通话，再使用聊天语音输入。");
      return;
    }
    if (CM.dom.input?.disabled) return;
    setButton("starting");
    try {
      await checkAsr();
      const stream = await navigator.mediaDevices.getUserMedia({
        audio:{echoCancellation:true, noiseSuppression:true, autoGainControl:true},
      });
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      const context = new AudioContextClass();
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(2048, 1, 1);
      processor.onaudioprocess = audioFrame;
      source.connect(processor);
      processor.connect(context.destination);
      state.stream = stream;
      state.audioContext = context;
      state.sourceNode = source;
      state.processor = processor;
      state.chunks = [];
      state.recording = true;
      setButton("recording");
    } catch (error) {
      cleanupCapture();
      setButton("idle");
      alert(`无法开始语音输入：${error.message}`);
    }
  }

  async function stopRecording({recognize = true} = {}) {
    if (!state.recording) return;
    const chunks = state.chunks;
    const sourceRate = state.audioContext?.sampleRate || 48000;
    state.chunks = [];
    cleanupCapture();
    setButton("idle");
    if (recognize) await transcribe(chunks, sourceRate);
  }

  async function toggleRecording() {
    if (state.recording) await stopRecording({recognize:true});
    else await startRecording();
  }

  button?.addEventListener("click", event => {
    event.preventDefault();
    toggleRecording().catch(console.error);
  });
  document.getElementById("voiceCallButton")?.addEventListener("click", () => {
    if (state.recording) stopRecording({recognize:false}).catch(console.error);
  }, {capture:true});
  CM.on("conversationChanged", () => {
    if (state.recording) stopRecording({recognize:false}).catch(console.error);
  });
  window.addEventListener("beforeunload", cleanupCapture);
  setButton("idle");
  CM.registerFeature("dictation", {start:startRecording, stop:stopRecording, state});
})();
