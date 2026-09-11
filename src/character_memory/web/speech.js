(() => {
  const CM = window.CM;
  const button = document.getElementById("speechButton");
  const status = document.getElementById("speechStatus");
  if (!CM || !button || !status) return;

  const state = {
    recorder: null,
    stream: null,
    chunks: [],
    startedAt: 0,
    timer: null,
    starting: false,
    transcribing: false,
    disabled: false,
  };

  const setStatus = (text, kind = "") => {
    status.textContent = text || "";
    status.dataset.kind = kind;
  };

  const updateButton = () => {
    const recording = state.recorder?.state === "recording";
    button.disabled = state.disabled || state.starting || state.transcribing;
    button.classList.toggle("recording", recording);
    button.setAttribute("aria-pressed", recording ? "true" : "false");
    button.title = recording ? "点击停止并转录" : "点击开始录音";
    button.textContent = recording ? "■" : "🎙";
  };

  const lockSend = locked => {
    if (locked) CM.dom.sendButton.disabled = true;
    else CM.updateComposerState?.();
  };

  const stopTracks = () => {
    for (const track of state.stream?.getTracks?.() || []) track.stop();
    state.stream = null;
  };

  const stopTimer = () => {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  };

  const renderRecordingTime = () => {
    const elapsed = Math.max(0, Math.floor((performance.now() - state.startedAt) / 1000));
    const minutes = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const seconds = String(elapsed % 60).padStart(2, "0");
    setStatus(`录音中 ${minutes}:${seconds} · 点击停止`, "recording");
  };

  const supportedMimeType = () => {
    const candidates = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/mp4",
    ];
    return candidates.find(value => MediaRecorder.isTypeSupported?.(value)) || "";
  };

  const insertTranscript = text => {
    const input = CM.dom.input;
    const value = input.value || "";
    const start = Number.isInteger(input.selectionStart) ? input.selectionStart : value.length;
    const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : start;
    const before = value.slice(0, start);
    const after = value.slice(end);
    const lead = before && !/\s$/.test(before) ? " " : "";
    const tail = after && !/^\s/.test(after) ? " " : "";
    const inserted = `${lead}${text}${tail}`;
    input.value = `${before}${inserted}${after}`;
    const caret = before.length + inserted.length;
    input.dispatchEvent(new Event("input", {bubbles:true}));
    input.focus();
    input.setSelectionRange?.(caret, caret);
  };

  const parseError = async response => {
    try {
      const payload = await response.json();
      return payload?.detail || JSON.stringify(payload);
    } catch (_) {
      return (await response.text()) || `${response.status} ${response.statusText}`;
    }
  };

  const transcribe = async blob => {
    state.transcribing = true;
    updateButton();
    lockSend(true);
    setStatus("正在使用本地 FunASR 转录…", "working");
    try {
      const character = encodeURIComponent(CM.state.characterId || "");
      const response = await fetch(`/v1/speech/transcribe?character_id=${character}`, {
        method: "POST",
        headers: {"Content-Type": blob.type || "audio/webm"},
        body: blob,
      });
      if (!response.ok) throw new Error(await parseError(response));
      const result = await response.json();
      const text = String(result.text || "").trim();
      if (!text) throw new Error("未识别到可用文本");
      insertTranscript(text);
      setStatus(`✓ 已转成文字（${CM.fmtMs(result.transcription_ms)}），请确认后发送`, "success");
    } catch (error) {
      setStatus(`语音识别失败：${error.message}`, "error");
      console.error("speech transcription failed", error);
    } finally {
      state.transcribing = false;
      lockSend(false);
      updateButton();
    }
  };

  const finishRecording = async () => {
    stopTimer();
    stopTracks();
    const mimeType = state.recorder?.mimeType || state.chunks[0]?.type || "audio/webm";
    const blob = new Blob(state.chunks, {type:mimeType});
    state.recorder = null;
    state.chunks = [];
    updateButton();
    if (!blob.size) {
      setStatus("没有录到声音，请重试", "error");
      lockSend(false);
      return;
    }
    await transcribe(blob);
  };

  const startRecording = async () => {
    if (state.disabled || state.starting || state.transcribing || state.recorder?.state === "recording") return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setStatus("当前浏览器不支持录音", "error");
      state.disabled = true;
      updateButton();
      return;
    }
    state.starting = true;
    updateButton();
    setStatus("正在请求麦克风…", "working");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {echoCancellation:true, noiseSuppression:true, autoGainControl:true},
        video: false,
      });
      const mimeType = supportedMimeType();
      const recorder = mimeType ? new MediaRecorder(stream, {mimeType}) : new MediaRecorder(stream);
      state.stream = stream;
      state.recorder = recorder;
      state.chunks = [];
      recorder.addEventListener("dataavailable", event => {
        if (event.data?.size) state.chunks.push(event.data);
      });
      recorder.addEventListener("stop", () => finishRecording().catch(console.error), {once:true});
      recorder.addEventListener("error", event => {
        setStatus(`录音失败：${event.error?.message || "未知错误"}`, "error");
        stopTimer();
        stopTracks();
        lockSend(false);
      }, {once:true});
      recorder.start();
      state.startedAt = performance.now();
      lockSend(true);
      renderRecordingTime();
      state.timer = setInterval(renderRecordingTime, 250);
    } catch (error) {
      stopTracks();
      setStatus(`无法使用麦克风：${error.message}`, "error");
    } finally {
      state.starting = false;
      updateButton();
    }
  };

  const stopRecording = () => {
    if (state.recorder?.state !== "recording") return;
    setStatus("录音结束，正在准备转录…", "working");
    stopTimer();
    state.recorder.stop();
    updateButton();
  };

  button.addEventListener("click", () => {
    if (state.recorder?.state === "recording") stopRecording();
    else startRecording().catch(console.error);
  });

  CM.on("ready", async () => {
    try {
      const response = await fetch("/v1/speech/status");
      if (!response.ok) return;
      const info = await response.json();
      if (!info.enabled) {
        state.disabled = true;
        setStatus("本地语音识别未启用");
      }
      updateButton();
    } catch (_) {
      // Chat remains usable even when the optional ASR surface is unavailable.
    }
  });

  CM.registerFeature("speech", {
    setDisabled(disabled) {
      state.disabled = Boolean(disabled);
      updateButton();
    },
    isRecording() {
      return state.recorder?.state === "recording";
    },
  });

  updateButton();
})();
