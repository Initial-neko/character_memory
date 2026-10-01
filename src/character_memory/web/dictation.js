(() => {
  const CM = window.CM;
  if (!CM) return;

  const mediaAudio = CM.mediaAudio;
  if (!mediaAudio) {
    console.error("Shared media audio module is unavailable");
    return;
  }
  const {mediaBase, concatChunks, downsample, wavBlob} = mediaAudio;
  const button = document.getElementById("voiceInputButton");
  const callButton = document.getElementById("voiceCallButton");
  const state = {
    recording: false,
    busy: false,
    stream: null,
    audioContext: null,
    sourceNode: null,
    processor: null,
    chunks: [],
    streaming: false,
    asrSession: null,
    finalTexts: [],
    partialText: "",
    streamError: null,
  };

  // A start that is still waiting on the permission prompt is only wanted until
  // the call takes the microphone. The generation token prevents a late browser
  // permission answer from adopting a stream in the wrong conversation mode.
  let wantedGeneration = 0;
  const callOwnsMicrophone = () => Boolean(CM.features.voice?.state?.active);

  function setButton(mode) {
    if (!button) return;
    button.classList.toggle("recording", mode === "recording");
    button.classList.toggle("transcribing", mode === "transcribing");
    button.disabled = mode === "starting" || mode === "transcribing";
    if (mode === "recording") {
      button.textContent = "■";
      button.title = state.partialText
        ? `正在听：${state.partialText.slice(-48)}`
        : (state.streaming ? "停止并完成流式识别" : "停止录音并识别");
      button.setAttribute("aria-label", "停止录音并识别");
      return;
    }
    if (mode === "transcribing") {
      button.textContent = "…";
      button.title = "正在完成语音识别";
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

  function releaseAudioCapture() {
    mediaAudio.closeCapture({
      stream:state.stream,
      context:state.audioContext,
      source:state.sourceNode,
      processor:state.processor,
    });
    state.stream = null;
    state.audioContext = null;
    state.sourceNode = null;
    state.processor = null;
    state.recording = false;
    state.chunks = [];
  }

  function resetStreamingState() {
    state.streaming = false;
    state.asrSession = null;
    state.finalTexts = [];
    state.partialText = "";
    state.streamError = null;
  }

  function cleanupCapture() {
    try { state.asrSession?.cancel?.(); } catch (_) {}
    releaseAudioCapture();
    resetStreamingState();
    state.busy = false;
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

  async function transcribeBatch(chunks, sourceRate) {
    if (!chunks.length) return;
    state.busy = true;
    setButton("transcribing");
    try {
      const raw = concatChunks(chunks);
      if (raw.length < Math.max(1, Math.floor(sourceRate * 0.15))) throw new Error("录音太短");
      const pcm = downsample(raw, sourceRate, 16000);
      const response = await fetch(`${mediaBase()}/v1/asr`, {
        method: "POST",
        headers: {"Content-Type":"audio/wav", "X-ASR-Source":"dictation"},
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
    if (!state.recording || state.streaming) return;
    state.chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
  }

  function canStream(health, context) {
    return Boolean(
      health?.asr?.streaming &&
      window.StreamingAsr?.createSession &&
      context?.audioWorklet &&
      window.AudioWorkletNode
    );
  }

  async function attachStreaming(context, source) {
    const session = window.StreamingAsr.createSession({
      mediaBase: mediaBase(),
      source: "dictation",
      onPartial(data) {
        if (state.asrSession !== session) return;
        state.partialText = String(data.text || "").trim();
        if (state.recording) setButton("recording");
      },
      onFinal(data) {
        if (state.asrSession !== session) return;
        const text = String(data.text || "").trim();
        if (text) state.finalTexts.push(text);
        state.partialText = "";
        if (state.recording) setButton("recording");
      },
      onError(error) {
        if (state.asrSession === session) state.streamError = error;
      },
    });
    state.asrSession = session;
    await session.connect();
    await session.attach(context, source);
    state.streaming = true;
  }

  async function startRecording() {
    if (state.recording || state.busy) return;
    if (callOwnsMicrophone()) {
      alert("请先结束语音通话，再使用聊天语音输入。");
      return;
    }
    if (CM.dom.input?.disabled) return;
    setButton("starting");
    const generation = wantedGeneration;
    try {
      const health = await checkAsr();
      if (generation !== wantedGeneration) {
        setButton("idle");
        return;
      }
      const stream = await mediaAudio.requestMicrophone();
      if (generation !== wantedGeneration || callOwnsMicrophone()) {
        mediaAudio.releaseStream(stream);
        setButton("idle");
        return;
      }

      const {context, source} = mediaAudio.createCapture(stream);
      state.stream = stream;
      state.audioContext = context;
      state.sourceNode = source;
      state.finalTexts = [];
      state.partialText = "";
      state.streamError = null;

      if (canStream(health, context)) {
        await attachStreaming(context, source);
      } else {
        const processor = context.createScriptProcessor(2048, 1, 1);
        processor.onaudioprocess = audioFrame;
        source.connect(processor);
        processor.connect(context.destination);
        state.processor = processor;
        state.streaming = false;
      }

      if (generation !== wantedGeneration || callOwnsMicrophone()) {
        cleanupCapture();
        setButton("idle");
        return;
      }
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

    if (state.streaming && state.asrSession) {
      const session = state.asrSession;
      state.recording = false;

      if (!recognize) {
        try { session.cancel(); } catch (_) {}
        releaseAudioCapture();
        resetStreamingState();
        setButton("idle");
        return;
      }

      state.busy = true;
      setButton("transcribing");
      try {
        await session.pauseInput();
        // pauseInput keeps the port alive for one task turn, so every PCM frame
        // already posted by the audio thread reaches the socket before flush.
        releaseAudioCapture();
        await session.flush("user_stop");
        if (state.streamError) throw state.streamError;
        const text = state.finalTexts.map(item => String(item || "").trim()).filter(Boolean).join("\n").trim();
        if (!text) throw new Error("没有识别到文字");
        insertTranscript(text);
      } catch (error) {
        alert(`语音识别失败：${error.message}`);
      } finally {
        try { session.close(); } catch (_) {}
        resetStreamingState();
        state.busy = false;
        setButton("idle");
      }
      return;
    }

    const chunks = state.chunks;
    const sourceRate = state.audioContext?.sampleRate || 48000;
    state.chunks = [];
    releaseAudioCapture();
    setButton("idle");
    if (recognize) await transcribeBatch(chunks, sourceRate);
  }

  async function toggleRecording() {
    if (state.recording) await stopRecording({recognize:true});
    else await startRecording();
  }

  button?.addEventListener("click", event => {
    event.preventDefault();
    toggleRecording().catch(console.error);
  });
  callButton?.addEventListener("click", () => {
    wantedGeneration += 1;
    if (state.recording) stopRecording({recognize:false}).catch(console.error);
  }, {capture:true});
  CM.on("conversationChanged", () => {
    wantedGeneration += 1;
    if (state.recording) stopRecording({recognize:false}).catch(console.error);
  });
  window.addEventListener("beforeunload", cleanupCapture);
  setButton("idle");
  CM.registerFeature("dictation", {start:startRecording, stop:stopRecording, state});
})();
