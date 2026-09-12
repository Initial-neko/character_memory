(() => {
  const CM = window.CM;
  if (!CM) return;

  const MEDIA_BASE_KEY = "character-memory:media-base-url";
  const mediaBase = () => localStorage.getItem(MEDIA_BASE_KEY) || "http://127.0.0.1:8001";
  const voice = {
    active: false,
    phase: "idle",
    stream: null,
    audioContext: null,
    sourceNode: null,
    processor: null,
    eventSource: null,
    chunks: [],
    preRoll: [],
    speechStartedAt: 0,
    lastVoiceAt: 0,
    hotFrames: 0,
    queue: [],
    playing: false,
    turnStartedAt: 0,
    lastMetrics: {},
    threshold: 0.025,
    silenceMs: 450,
    minSpeechMs: 250,
    maxSpeechMs: 12000,
  };

  const dom = {
    button: document.getElementById("voiceCallButton"),
    overlay: document.getElementById("voiceCallOverlay"),
    avatar: document.getElementById("voiceCallAvatar"),
    name: document.getElementById("voiceCallName"),
    status: document.getElementById("voiceCallStatus"),
    transcript: document.getElementById("voiceCallTranscript"),
    metrics: document.getElementById("voiceCallMetrics"),
    hangup: document.getElementById("voiceHangupButton"),
  };

  function setPhase(phase, text) {
    voice.phase = phase;
    if (dom.status) dom.status.textContent = text || phase;
  }

  function formatMetrics() {
    const m = voice.lastMetrics;
    const parts = [];
    if (m.asr != null) parts.push(`ASR ${Math.round(m.asr)}ms`);
    if (m.llm != null) parts.push(`LLM ${Math.round(m.llm)}ms`);
    if (m.tts != null) parts.push(`TTS ${Math.round(m.tts)}ms`);
    if (m.total != null) parts.push(`总计 ${Math.round(m.total)}ms`);
    if (dom.metrics) dom.metrics.textContent = parts.join(" · ");
  }

  function rms(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
    return Math.sqrt(sum / Math.max(1, samples.length));
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
    return new Blob([buffer], {type: "audio/wav"});
  }

  async function checkMedia() {
    const response = await fetch(`${mediaBase()}/health`);
    if (!response.ok) throw new Error(`Media Runtime ${response.status}`);
    const health = await response.json();
    if (!health.asr?.ready) throw new Error(health.asr?.reason || "ASR 未配置");
    if (!health.tts?.ready) throw new Error(health.tts?.reason || "TTS 未配置");
    return health;
  }

  function openVoiceEvents() {
    voice.eventSource?.close?.();
    const characterId = CM.state.characterId;
    const conversationId = CM.conversationIdFor(characterId);
    const params = new URLSearchParams({scope:"direct", character_id:characterId, conversation_id:conversationId});
    const source = new EventSource(`/v1/events/stream?${params.toString()}`);
    voice.eventSource = source;
    source.addEventListener("reaction_status", event => {
      if (!voice.active) return;
      const data = JSON.parse(event.data || "{}");
      if (data.state === "typing" && voice.phase === "waiting") {
        setPhase("waiting", "正在想…");
      }
    });
    source.addEventListener("character_event", event => {
      if (!voice.active) return;
      const data = JSON.parse(event.data || "{}");
      const action = String(data.metadata?.action || "").toUpperCase();
      if (action === "MESSAGE" && String(data.content || "").trim()) {
        if (voice.lastMetrics.llm == null && voice.turnStartedAt) {
          voice.lastMetrics.llm = performance.now() - voice.turnStartedAt - Number(voice.lastMetrics.asr || 0);
          formatMetrics();
        }
        voice.queue.push(String(data.content).trim());
        playQueue();
      }
    });
    source.addEventListener("reaction_complete", () => {
      if (!voice.active) return;
      if (!voice.playing && voice.queue.length === 0 && voice.phase === "waiting") {
        setPhase("listening", "正在听…");
      }
    });
    source.addEventListener("reaction_error", event => {
      if (!voice.active) return;
      let message = "角色响应失败";
      try { message = JSON.parse(event.data || "{}").message || message; } catch (_) {}
      setPhase("error", message);
      setTimeout(() => voice.active && setPhase("listening", "正在听…"), 1200);
    });
  }

  async function sendTranscript(text) {
    const characterId = CM.state.characterId;
    const conversationId = CM.conversationIdFor(characterId);
    const response = await fetch("/v1/chat/messages", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({message:text, character_id:characterId, conversation_id:conversationId}),
    });
    if (!response.ok) throw new Error(await response.text());
    const result = await response.json();
    if (result.message) CM.mergeDirectMessage?.(result.message);
  }

  async function finishSpeech() {
    if (!voice.active || !voice.chunks.length) return;
    const chunks = voice.chunks;
    voice.chunks = [];
    voice.preRoll = [];
    voice.hotFrames = 0;
    setPhase("transcribing", "识别中…");
    const sourceRate = voice.audioContext.sampleRate;
    const raw = concatChunks(chunks);
    const pcm = downsample(raw, sourceRate, 16000);
    const blob = wavBlob(pcm, 16000);
    const asrStarted = performance.now();
    try {
      const response = await fetch(`${mediaBase()}/v1/asr`, {
        method: "POST",
        headers: {"Content-Type":"audio/wav"},
        body: blob,
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      voice.lastMetrics = {asr: performance.now() - asrStarted};
      voice.turnStartedAt = performance.now();
      formatMetrics();
      const text = String(result.text || "").trim();
      if (!text) throw new Error("没有识别到文字");
      if (dom.transcript) dom.transcript.textContent = `你：${text}`;
      setPhase("waiting", "正在想…");
      await sendTranscript(text);
    } catch (error) {
      setPhase("error", `语音失败：${error.message}`);
      setTimeout(() => voice.active && setPhase("listening", "正在听…"), 1200);
    }
  }

  async function synthesize(text) {
    const profileIndex = Math.max(0, CM.state.characters.findIndex(item => item.id === CM.state.characterId));
    const speakerId = profileIndex % 5;
    const started = performance.now();
    const response = await fetch(`${mediaBase()}/v1/tts`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({text, speaker_id:speakerId, speed:1.0}),
    });
    if (!response.ok) throw new Error(await response.text());
    const blob = await response.blob();
    voice.lastMetrics.tts = performance.now() - started;
    formatMetrics();
    return URL.createObjectURL(blob);
  }

  async function playQueue() {
    if (!voice.active || voice.playing) return;
    voice.playing = true;
    try {
      while (voice.active && voice.queue.length) {
        const text = voice.queue.shift();
        setPhase("speaking", "正在说…");
        const url = await synthesize(text);
        try {
          await new Promise((resolve, reject) => {
            const audio = new Audio(url);
            audio.onended = resolve;
            audio.onerror = reject;
            audio.play().catch(reject);
          });
        } finally {
          URL.revokeObjectURL(url);
        }
      }
      if (voice.active) {
        voice.lastMetrics.total = voice.turnStartedAt ? performance.now() - voice.turnStartedAt + Number(voice.lastMetrics.asr || 0) : null;
        formatMetrics();
        setPhase("listening", "正在听…");
      }
    } catch (error) {
      if (voice.active) {
        setPhase("error", `TTS 失败：${error.message}`);
        setTimeout(() => voice.active && setPhase("listening", "正在听…"), 1200);
      }
    } finally {
      voice.playing = false;
    }
  }

  function audioFrame(event) {
    if (!voice.active || !["listening", "recording"].includes(voice.phase)) return;
    const input = event.inputBuffer.getChannelData(0);
    const chunk = new Float32Array(input);
    const level = rms(chunk);
    const now = performance.now();

    if (voice.phase === "listening") {
      voice.preRoll.push(chunk);
      while (voice.preRoll.length > 6) voice.preRoll.shift();
      if (level >= voice.threshold) voice.hotFrames += 1;
      else voice.hotFrames = 0;
      if (voice.hotFrames >= 2) {
        voice.chunks = [...voice.preRoll];
        voice.preRoll = [];
        voice.speechStartedAt = now;
        voice.lastVoiceAt = now;
        setPhase("recording", "正在听你说…");
      }
      return;
    }

    voice.chunks.push(chunk);
    if (level >= voice.threshold * 0.7) voice.lastVoiceAt = now;
    const spokenMs = now - voice.speechStartedAt;
    if (
      spokenMs >= voice.minSpeechMs &&
      (now - voice.lastVoiceAt >= voice.silenceMs || spokenMs >= voice.maxSpeechMs)
    ) {
      finishSpeech();
    }
  }

  async function startCall() {
    if (voice.active || CM.isGroupConversation()) return;
    dom.button.disabled = true;
    try {
      await checkMedia();
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

      voice.active = true;
      voice.stream = stream;
      voice.audioContext = context;
      voice.sourceNode = source;
      voice.processor = processor;
      voice.queue = [];
      voice.playing = false;
      voice.lastMetrics = {};
      voice.preRoll = [];
      voice.chunks = [];
      openVoiceEvents();

      const profile = CM.currentProfile();
      if (dom.avatar) dom.avatar.textContent = CM.initialFor(profile);
      if (dom.name) dom.name.textContent = profile.name || profile.id;
      if (dom.transcript) dom.transcript.textContent = "直接说话即可；停顿后会自动发送。";
      if (dom.metrics) dom.metrics.textContent = "";
      dom.overlay?.classList.remove("hidden");
      setPhase("listening", "正在听…");
    } catch (error) {
      alert(`无法开始语音：${error.message}`);
    } finally {
      dom.button.disabled = false;
    }
  }

  async function stopCall() {
    voice.active = false;
    voice.eventSource?.close?.();
    voice.eventSource = null;
    voice.processor?.disconnect?.();
    voice.sourceNode?.disconnect?.();
    voice.stream?.getTracks?.().forEach(track => track.stop());
    try { await voice.audioContext?.close?.(); } catch (_) {}
    voice.stream = null;
    voice.audioContext = null;
    voice.sourceNode = null;
    voice.processor = null;
    voice.queue = [];
    voice.chunks = [];
    voice.preRoll = [];
    voice.playing = false;
    setPhase("idle", "");
    dom.overlay?.classList.add("hidden");
  }

  dom.button?.addEventListener("click", startCall);
  dom.hangup?.addEventListener("click", stopCall);
  window.addEventListener("beforeunload", () => { if (voice.active) stopCall(); });
  CM.on("conversationChanged", () => { if (voice.active) stopCall(); });
  CM.registerFeature("voice", {start:startCall, stop:stopCall, state:voice});
})();
