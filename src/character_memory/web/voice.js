(() => {
  const CM = window.CM;
  if (!CM) return;

  const mediaAudio = CM.mediaAudio;
  if (!mediaAudio) {
    console.error("Shared media audio module is unavailable");
    return;
  }
  const {mediaBase, concatChunks, downsample, wavBlob} = mediaAudio;
  const TTS_SPEAKER_IDS = [0, 2, 5];
  const SPEAKABLE_ACTIONS = new Set(["MESSAGE", "REPLY", "MINIMAL_RESPONSE", "PROACTIVE_MESSAGE"]);
  // Used only until /health answers; the runtime owns the real value.
  const DEFAULT_SILENCE_MS = 900;

  const voice = {
    active: false,
    micActive: false,
    minimized: false,
    phase: "idle",
    capturePhase: "idle",
    target: null,
    currentSpeakerId: null,
    stream: null,
    audioContext: null,
    sourceNode: null,
    processor: null,
    asrSession: null,
    streamingAsr: false,
    streamingSegmentId: null,
    streamingSegmentStartedAt: 0,
    seenAsrSegments: new Set(),
    eventSource: null,
    visualSession: null,
    periodicVisualTimer: null,
    periodicVisualConfig: {enabled:false, intervalSeconds:30, maxPerHour:0},
    periodicVisualLastFrameAt: 0,
    periodicVisualSending: false,
    currentAudio: null,
    chunks: [],
    preRoll: [],
    speechStartedAt: 0,
    lastVoiceAt: 0,
    hotFrames: 0,
    queue: [],
    pendingTurns: [],
    playing: false,
    ttsTail: Promise.resolve(),
    turnStartedAt: 0,
    lastMetrics: {},
    speakerId: 0,
    callMessageIds: new Set(),
    threshold: 0.025,
    silenceMs: DEFAULT_SILENCE_MS,
    minSpeechMs: 250,
    maxSpeechMs: 12000,
  };

  const dom = {
    button: document.getElementById("voiceCallButton"),
    overlay: document.getElementById("voiceCallOverlay"),
    avatar: document.getElementById("voiceCallAvatar"),
    name: document.getElementById("voiceCallName"),
    context: document.getElementById("voiceCallContext"),
    status: document.getElementById("voiceCallStatus"),
    transcript: document.getElementById("voiceCallTranscript"),
    log: document.getElementById("voiceCallLog"),
    metrics: document.getElementById("voiceCallMetrics"),
    minimize: document.getElementById("voiceMinimizeButton"),
    hangup: document.getElementById("voiceHangupButton"),
    dock: document.getElementById("voiceCallDock"),
    dockExpand: document.getElementById("voiceDockExpandButton"),
    dockAvatar: document.getElementById("voiceDockAvatar"),
    dockTitle: document.getElementById("voiceDockTitle"),
    dockStatus: document.getElementById("voiceDockStatus"),
    dockVisual: document.getElementById("voiceDockVisual"),
    dockHangup: document.getElementById("voiceDockHangupButton"),
    mic: document.getElementById("voiceMicButton"),
    camera: document.getElementById("voiceCameraButton"),
    screen: document.getElementById("voiceScreenButton"),
    visualStop: document.getElementById("voiceVisualStopButton"),
    visualPanel: document.getElementById("voiceVisualPanel"),
    visualPreview: document.getElementById("voiceVisualPreview"),
    visualStatus: document.getElementById("voiceVisualStatus"),
  };

  function profileFor(characterId) {
    return CM.state.characters.find(item => item.id === characterId) || null;
  }

  function groupMemberName(characterId) {
    const member = voice.target?.members?.find(item => item.id === characterId);
    return member?.name || characterId;
  }

  function speakerName(characterId) {
    const profile = profileFor(characterId);
    return profile?.name || groupMemberName(characterId) || characterId || "角色";
  }

  function stableSpeakerId(characterId) {
    let hash = 2166136261;
    for (const char of String(characterId || "default")) {
      hash ^= char.codePointAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return TTS_SPEAKER_IDS[(hash >>> 0) % TTS_SPEAKER_IDS.length];
  }

  function setAvatar(container, characterId = null, fallback = "AI") {
    if (!container) return;
    const profile = characterId ? profileFor(characterId) : null;
    const label = profile?.name || (characterId ? speakerName(characterId) : fallback) || "AI";
    const initial = String(label).trim().slice(0, 1).toUpperCase() || "AI";
    container.innerHTML = "";
    if (profile?.avatar_url) {
      const img = document.createElement("img");
      img.className = "voice-call-avatar-image";
      img.src = profile.avatar_url;
      img.alt = `${label} 头像`;
      img.addEventListener("error", () => { container.textContent = initial; }, {once:true});
      container.appendChild(img);
    } else {
      container.textContent = initial;
    }
  }

  function captureTarget() {
    if (CM.isGroupConversation()) {
      const group = CM.features.groups?.current?.();
      if (!group?.id) throw new Error("当前群聊尚未准备好");
      return {
        scope: "group",
        conversationId: group.id,
        groupId: group.id,
        title: group.name || "群聊",
        members: (group.members || []).map(item => ({id:item.id, name:item.name || item.id})),
      };
    }
    const profile = CM.currentProfile();
    return {
      scope: "direct",
      conversationId: CM.conversationIdFor(profile.id),
      characterId: profile.id,
      title: profile.name || profile.id,
      members: [{id:profile.id, name:profile.name || profile.id}],
    };
  }

  function isViewingTarget() {
    const target = voice.target;
    if (!target) return false;
    if (target.scope === "group") {
      return CM.isGroupConversation() && CM.state.conversation.groupId === target.conversationId;
    }
    return !CM.isGroupConversation() && CM.state.characterId === target.characterId;
  }

  function renderCallIdentity() {
    const target = voice.target;
    if (!target) return;
    const speakingId = voice.currentSpeakerId;
    if (target.scope === "group") {
      if (speakingId) {
        const name = speakerName(speakingId);
        setAvatar(dom.avatar, speakingId, target.title);
        setAvatar(dom.dockAvatar, speakingId, target.title);
        if (dom.name) dom.name.textContent = name;
        if (dom.context) dom.context.textContent = `群聊 · ${target.title}`;
        if (dom.dockTitle) dom.dockTitle.textContent = `${name} · ${target.title}`;
      } else {
        setAvatar(dom.avatar, null, target.title);
        setAvatar(dom.dockAvatar, null, target.title);
        if (dom.name) dom.name.textContent = target.title;
        if (dom.context) dom.context.textContent = "群聊语音";
        if (dom.dockTitle) dom.dockTitle.textContent = target.title;
      }
    } else {
      const id = target.characterId;
      const name = speakerName(id);
      setAvatar(dom.avatar, id, name);
      setAvatar(dom.dockAvatar, id, name);
      if (dom.name) dom.name.textContent = name;
      if (dom.context) dom.context.textContent = "单聊通话";
      if (dom.dockTitle) dom.dockTitle.textContent = name;
    }
  }

  function updateCallButton() {
    if (!dom.button) return;
    dom.button.classList.toggle("active", voice.active);
    dom.button.title = voice.active ? "返回正在进行的通话" : "语音/视频通话";
  }

  function setPhase(phase, text) {
    voice.phase = phase;
    const label = text || phase;
    if (dom.status) dom.status.textContent = label;
    if (dom.dockStatus) dom.dockStatus.textContent = label;
  }

  function setCapturePhase(phase) {
    voice.capturePhase = phase;
  }

  function updateMicUi() {
    if (!dom.mic) return;
    const active = Boolean(voice.active && voice.micActive);
    dom.mic.classList.toggle("active", active);
    dom.mic.classList.toggle("muted", voice.active && !voice.micActive);
    dom.mic.setAttribute?.("aria-pressed", active ? "true" : "false");
    dom.mic.textContent = active ? "🎙 麦克风" : "🔇 麦克风";
    dom.mic.title = active ? "关闭麦克风" : "开启麦克风";
    dom.mic.disabled = !voice.active;
  }

  // Hands capture back to the VAD, unless it is already busy with the person's own speech.
  //
  // A reset that lands mid-utterance does not merely change a label: capturePhase decides which
  // branch of audioFrame runs, and the listening branch starts a fresh preRoll and then
  // *overwrites* voice.chunks with it. Everything buffered before the reset -- the words already
  // said -- is gone, and what gets transcribed is the tail of the sentence. A reset that lands
  // mid-transcription stacks a second turn on the first.
  //
  // The character's reply arrives on its own schedule and does not wait for the person to finish
  // a sentence, so every writer that means "go back to listening" has to come through here.
  // finishSpeech is what actually ends an utterance, and it always restores capture itself.
  function resumeCapture() {
    if (voice.capturePhase === "recording" || voice.capturePhase === "transcribing") return;
    setCapturePhase(voice.micActive ? "listening" : "idle");
  }

  function resumeInputState() {
    if (!voice.active) return;
    if (voice.micActive) {
      // Still mid-sentence: the status belongs to the utterance, not to the idle call.
      if (voice.capturePhase === "recording") {
        setPhase("recording", "正在听你说…");
        return;
      }
      resumeCapture();
      setPhase("listening", "正在听…");
      return;
    }
    setCapturePhase("idle");
    const visual = voice.visualSession?.getState?.() || {active:false, source:null};
    if (visual.active) {
      setPhase("muted", (visual.source === "DISPLAY" ? "屏幕共享" : "摄像头") + "中 · 麦克风已关闭");
    } else {
      setPhase("muted", "麦克风已关闭");
    }
  }

  function updateVisualUi(snapshot = null) {
    const value = snapshot || voice.visualSession?.getState?.() || {active:false, source:null, candidateCount:0};
    const active = Boolean(value.active);
    dom.camera?.classList.toggle("active", active && value.source === "CAMERA");
    dom.screen?.classList.toggle("active", active && value.source === "DISPLAY");
    dom.visualStop?.classList.toggle("hidden", !active);
    dom.visualPanel?.classList.toggle("hidden", !active);
    if (dom.dockVisual) {
      dom.dockVisual.classList.toggle("hidden", !active);
      dom.dockVisual.textContent = value.source === "CAMERA" ? "📷" : value.source === "DISPLAY" ? "🖥" : "";
      dom.dockVisual.title = active ? `${value.source === "CAMERA" ? "摄像头" : "屏幕共享"} · ${value.candidateCount || 0} 个候选帧` : "";
    }
    if (voice.active && !voice.micActive && !["waiting", "speaking", "recording"].includes(voice.phase)) {
      resumeInputState();
    }
  }

  function ensureVisualSession() {
    if (voice.visualSession) return voice.visualSession;
    if (!window.VisualCapture?.createSession) throw new Error("Visual Capture 模块未加载");
    voice.visualSession = window.VisualCapture.createSession({
      preview:dom.visualPreview,
      status:dom.visualStatus,
      onStateChange:updateVisualUi,
    });
    updateVisualUi();
    return voice.visualSession;
  }

  function stopPeriodicVisualObservation() {
    if (voice.periodicVisualTimer != null) {
      clearInterval(voice.periodicVisualTimer);
      voice.periodicVisualTimer = null;
    }
    voice.periodicVisualSending = false;
  }

  async function loadPeriodicVisualConfig() {
    try {
      const response = await fetch("/v1/visual/periodic/config", {cache:"no-store"});
      if (!response.ok) throw new Error(`periodic visual config ${response.status}`);
      const data = await response.json();
      const interval = Number(data.interval_seconds);
      const maxPerHour = Number(data.max_per_hour);
      voice.periodicVisualConfig = {
        enabled:Boolean(data.enabled),
        intervalSeconds:Number.isFinite(interval) ? Math.max(10, Math.min(600, interval)) : 30,
        maxPerHour:Number.isFinite(maxPerHour) ? Math.max(0, maxPerHour) : 0,
      };
    } catch (error) {
      console.debug("periodic visual observation unavailable", error);
      voice.periodicVisualConfig = {enabled:false, intervalSeconds:30, maxPerHour:0};
    }
    return voice.periodicVisualConfig;
  }

  async function runPeriodicVisualObservation() {
    if (
      !voice.active ||
      voice.target?.scope !== "direct" ||
      !voice.periodicVisualConfig.enabled ||
      voice.periodicVisualConfig.maxPerHour <= 0 ||
      voice.periodicVisualSending ||
      replyInFlight() ||
      ["recording", "transcribing"].includes(voice.capturePhase)
    ) return;

    const session = voice.visualSession;
    const visual = session?.getState?.();
    if (!visual?.active || visual.source !== "DISPLAY") return;
    const frame = session.latestSignificantFrame?.({
      afterMs:voice.periodicVisualLastFrameAt,
      source:"DISPLAY",
    });
    if (!frame) return;

    // Consume the changed frame before the HTTP request. A busy/quota refusal
    // should not make a stale screen keep retrying forever; a later meaningful
    // change will create a new candidate and another opportunity.
    voice.periodicVisualLastFrameAt = Number(frame.captured_at_ms || performance.now());
    voice.periodicVisualSending = true;
    try {
      const response = await fetch("/v1/visual/direct/observations", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          character_id:voice.target.characterId,
          conversation_id:voice.target.conversationId,
          visual_frame:frame,
        }),
      });
      if (!response.ok) throw new Error(`periodic visual observation ${response.status}`);
      const data = await response.json();
      if (data.accepted) console.debug("periodic visual observation accepted", data.event_id);
    } catch (error) {
      console.debug("periodic visual observation failed", error);
    } finally {
      voice.periodicVisualSending = false;
    }
  }

  async function startPeriodicVisualObservation() {
    stopPeriodicVisualObservation();
    voice.periodicVisualLastFrameAt = 0;
    if (voice.target?.scope !== "direct") return;
    const config = await loadPeriodicVisualConfig();
    if (!config.enabled || config.maxPerHour <= 0) return;
    const delay = Math.max(10000, Math.round(config.intervalSeconds * 1000));
    voice.periodicVisualTimer = setInterval(() => {
      runPeriodicVisualObservation().catch(error => console.debug("periodic visual tick failed", error));
    }, delay);
  }

  async function startCameraVisual() {
    if (!voice.active) throw new Error("请先开始通话");
    stopPeriodicVisualObservation();
    const session = ensureVisualSession();
    await session.startCamera();
    updateVisualUi();
    resumeInputState();
  }

  async function startScreenVisual() {
    if (!voice.active) throw new Error("请先开始通话");
    const session = ensureVisualSession();
    await session.startDisplay();
    updateVisualUi();
    resumeInputState();
    await startPeriodicVisualObservation();
  }

  function stopVisual({clearCandidates = false} = {}) {
    stopPeriodicVisualObservation();
    voice.visualSession?.stop?.({clearCandidates, reason:"视觉已关闭"});
    updateVisualUi();
    resumeInputState();
  }

  function visualFramesForCurrentConversation() {
    if (!voice.active || !isViewingTarget()) return [];
    const session = voice.visualSession;
    if (!session?.getState?.().active) return [];
    const now = performance.now();
    return session.selectFrames({
      fromMs:Math.max(0, now - 15000),
      toMs:now,
      maxFrames:4,
    });
  }

  async function sendTextWithVisual(message) {
    const text = String(message || "").trim();
    if (!text) return {handled:false};
    const visualFrames = visualFramesForCurrentConversation();
    if (!visualFrames.length) return {handled:false};
    const sent = await sendTranscript(text, visualFrames);
    appendCallLog("user", text, sent.message?.id ?? sent.event_id ?? null);
    if (dom.transcript) dom.transcript.textContent = "你：" + text + " · 附 " + visualFrames.length + " 个视觉关键帧";
    return {handled:true, result:sent, frameCount:visualFrames.length};
  }

  function formatMetrics() {
    const m = voice.lastMetrics;
    const parts = [];
    if (voice.currentSpeakerId) parts.push(`Voice #${stableSpeakerId(voice.currentSpeakerId)}`);
    if (m.asr != null) parts.push(`ASR ${Math.round(m.asr)}ms`);
    if (m.llm != null) parts.push(`LLM ${Math.round(m.llm)}ms`);
    if (m.tts != null) parts.push(`TTS ${Math.round(m.tts)}ms`);
    if (m.total != null) parts.push(`总计 ${Math.round(m.total)}ms`);
    if (dom.metrics) dom.metrics.textContent = parts.join(" · ");
  }

  function resetCallLog() {
    voice.callMessageIds.clear();
    if (dom.log) dom.log.innerHTML = '<div class="voice-call-log-empty">开始说话后，这里会显示本次通话的文字内容。</div>';
  }

  function appendCallLog(role, text, messageId = null, actorId = null) {
    const value = String(text || "").trim();
    if (!value || !dom.log) return;
    if (messageId != null) {
      const key = `${role}:${messageId}`;
      if (voice.callMessageIds.has(key)) return;
      voice.callMessageIds.add(key);
    }
    dom.log.querySelector(".voice-call-log-empty")?.remove();
    const line = document.createElement("div");
    line.className = `voice-call-line ${role}`;
    const label = role === "user" ? "你" : speakerName(actorId);
    const labelEl = document.createElement("span");
    labelEl.className = "voice-call-line-role";
    labelEl.textContent = label;
    const textEl = document.createElement("div");
    textEl.className = "voice-call-line-text";
    textEl.textContent = value;
    line.append(labelEl, textEl);
    dom.log.appendChild(line);
    dom.log.scrollTop = dom.log.scrollHeight;
  }

  function minimizeCall() {
    if (!voice.active) return;
    voice.minimized = true;
    dom.overlay?.classList.add("hidden");
    dom.dock?.classList.remove("hidden");
  }

  function expandCall() {
    if (!voice.active) return;
    voice.minimized = false;
    dom.dock?.classList.add("hidden");
    dom.overlay?.classList.remove("hidden");
    renderCallIdentity();
  }

  function rms(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
    return Math.sqrt(sum / Math.max(1, samples.length));
  }

  async function checkMedia() {
    const response = await fetch(`${mediaBase()}/health`);
    if (!response.ok) throw new Error(`Media Runtime ${response.status}`);
    const health = await response.json();
    if (!health.asr?.ready) throw new Error(health.asr?.reason || "ASR 未配置");
    if (!health.tts?.ready) throw new Error(health.tts?.reason || "TTS 未配置");
    return health;
  }

  // The Media Runtime owns the endpointing threshold so it can be tuned from
  // Settings Center without a code change. Guard the shape: an older runtime
  // without the field, or a malformed value, must leave the default in place.
  function applyVoiceCapture(health) {
    const configured = Number(health?.voice_capture?.silence_ms);
    voice.silenceMs = Number.isFinite(configured) && configured >= 200 ? configured : DEFAULT_SILENCE_MS;
  }

  // A transcript with no Han character at all is the shape the recognizer's
  // hallucination takes on non-speech: a fan, a cough or a keyboard gets answered
  // with "Yeah." or "The.". The rule used to admit anything with two Latin
  // characters, which is exactly what those answers look like, so a Chinese-first
  // voice path now requires a Han character. A genuine purely non-Chinese utterance
  // is refused by this on purpose -- admitting one has to be a deliberate change.
  function validateAsrTranscript(raw) {
    const text = String(raw || "").trim();
    if (!text) return {valid:false, text:"", reason:"empty"};
    const meaningful = text.replace(/[\s\p{P}\p{S}]/gu, "");
    if (!meaningful) return {valid:false, text, reason:"punctuation_only"};
    if (/\p{Script=Han}/u.test(text)) return {valid:true, text, reason:"valid"};
    return {valid:false, text, reason:"no_han"};
  }

  function canStreamAsr(health, context) {
    return Boolean(
      health?.asr?.streaming &&
      window.StreamingAsr?.createSession &&
      context?.audioWorklet &&
      window.AudioWorkletNode
    );
  }

  function streamingSegmentKey(data) {
    return `${String(data?.session_id || "")}:${String(data?.segment_id ?? "")}`;
  }

  function handleStreamingPartial(data) {
    if (!voice.active || !voice.micActive || !voice.streamingAsr) return;
    const text = String(data?.text || "").trim();
    if (!text) return;
    const key = streamingSegmentKey(data);
    if (voice.streamingSegmentId !== key) {
      voice.streamingSegmentId = key;
      voice.streamingSegmentStartedAt = performance.now();
    }
    setCapturePhase("recording");
    if (dom.transcript && !replyInFlight()) dom.transcript.textContent = `你：${text} …`;
    if (!replyInFlight()) setPhase("recording", "正在听你说…");
  }

  async function handleStreamingFinal(data) {
    if (!voice.active || !voice.micActive || !voice.streamingAsr) return;
    const key = streamingSegmentKey(data);
    if (voice.seenAsrSegments.has(key)) return;
    voice.seenAsrSegments.add(key);
    if (voice.seenAsrSegments.size > 64) {
      voice.seenAsrSegments = new Set([...voice.seenAsrSegments].slice(-32));
    }

    const endedAt = performance.now();
    const startedAt = voice.streamingSegmentId === key && voice.streamingSegmentStartedAt
      ? voice.streamingSegmentStartedAt
      : Math.max(0, endedAt - 15000);
    voice.streamingSegmentId = null;
    voice.streamingSegmentStartedAt = 0;
    const validation = validateAsrTranscript(data?.text);
    setCapturePhase("listening");

    if (!validation.valid) {
      if (dom.transcript) dom.transcript.textContent = "没有识别到有效内容";
      if (!replyInFlight()) setPhase("listening", "正在听…");
      console.debug("[voice] ignored invalid streaming ASR transcript", validation.reason, validation.text);
      return;
    }

    const visualFrames = voice.visualSession?.selectFrames?.({
      fromMs:Math.max(0, startedAt - 1000),
      toMs:endedAt,
      maxFrames:4,
    }) || [];
    // Streaming finals do not expose a stable inference-only latency yet. Do not
    // manufacture a 0 ms metric; batch mode still reports its measured ASR time.
    const turn = {text:validation.text, visualFrames, asrMs:null};
    if (replyInFlight()) {
      voice.pendingTurns.push(turn);
      if (dom.transcript) {
        dom.transcript.textContent = `你：${validation.text} · 已听到，等这轮回应结束后发送…`;
      }
      return;
    }
    await dispatchRecognizedTurn(turn);
  }

  async function attachStreamingAsr(context, source) {
    const session = window.StreamingAsr.createSession({
      mediaBase: mediaBase(),
      source: "call",
      onPartial: handleStreamingPartial,
      onFinal(data) {
        handleStreamingFinal(data).catch(error => {
          console.warn("[voice] streaming final failed", error);
        });
      },
      onError(error) {
        if (!voice.active || voice.asrSession !== session) return;
        // An error during initial connect is handled by startMicrophone's fallback.
        // Once the call owns the mic, degrade in place instead of making the user
        // toggle hardware just because the WebSocket path failed.
        if (!voice.micActive) return;
        console.warn("[voice] streaming ASR failed; switching to batch capture", error);
        try { session.close(); } catch (_) {}
        voice.asrSession = null;
        voice.streamingAsr = false;
        voice.streamingSegmentId = null;
        voice.streamingSegmentStartedAt = 0;
        try {
          if (voice.audioContext && voice.sourceNode && !voice.processor) {
            attachBatchAsr(voice.audioContext, voice.sourceNode);
            setCapturePhase("listening");
            if (dom.transcript) dom.transcript.textContent = "流式语音连接异常，已切换到兼容识别模式。";
            if (!replyInFlight()) setPhase("listening", "正在听… · 兼容模式");
            return;
          }
        } catch (fallbackError) {
          console.warn("[voice] batch ASR fallback failed", fallbackError);
        }
        if (dom.transcript) dom.transcript.textContent = `流式语音连接异常：${error.message}`;
        if (!replyInFlight()) setPhase("error", "流式语音连接异常 · 可重开麦克风");
      },
    });
    voice.asrSession = session;
    await session.connect();
    await session.attach(context, source);
    voice.streamingAsr = true;
    voice.streamingSegmentId = null;
    voice.streamingSegmentStartedAt = 0;
    voice.seenAsrSegments = new Set();
    return session;
  }

  function attachBatchAsr(context, source) {
    const processor = context.createScriptProcessor(2048, 1, 1);
    processor.onaudioprocess = audioFrame;
    source.connect(processor);
    processor.connect(context.destination);
    voice.processor = processor;
    voice.streamingAsr = false;
    return processor;
  }

  function handleCharacterEvent(data, characterId) {
    if (!voice.active) return;
    const action = String(data.metadata?.action || "").toUpperCase();
    const text = String(data.content || "").trim();
    if (!SPEAKABLE_ACTIONS.has(action) || !text) return;

    if (voice.target?.scope === "direct" && isViewingTarget() && CM.directEventToMessage && CM.mergeDirectMessage) {
      CM.mergeDirectMessage(CM.directEventToMessage(data));
    }
    appendCallLog("assistant", text, data.id, characterId);
    if (dom.transcript) dom.transcript.textContent = `${speakerName(characterId)}：${text}`;
    if (voice.lastMetrics.llm == null && voice.turnStartedAt) {
      voice.lastMetrics.llm = performance.now() - voice.turnStartedAt - Number(voice.lastMetrics.asr || 0);
      formatMetrics();
    }
    voice.queue.push({text, characterId, messageId:data.id, audioPromise:null, audioUrl:null});
    resumeCapture();
    if (voice.playing) prefetchNext();
    playQueue();
  }

  function openVoiceEvents() {
    voice.eventSource?.close?.();
    const target = voice.target;
    if (!target) return;
    const params = new URLSearchParams({scope:target.scope, conversation_id:target.conversationId});
    if (target.scope === "direct") params.set("character_id", target.characterId);
    const source = new EventSource(`/v1/events/stream?${params.toString()}`);
    voice.eventSource = source;

    source.addEventListener("reaction_status", event => {
      if (!voice.active) return;
      const data = JSON.parse(event.data || "{}");
      if (data.state === "typing" && voice.phase === "waiting") setPhase("waiting", "正在想…");
    });

    if (target.scope === "group") {
      source.addEventListener("group_character_event", event => {
        if (!voice.active) return;
        const data = JSON.parse(event.data || "{}");
        handleCharacterEvent(data, data.actor_id);
      });
    } else {
      source.addEventListener("character_event", event => {
        if (!voice.active) return;
        const data = JSON.parse(event.data || "{}");
        handleCharacterEvent(data, data.character_id || target.characterId);
      });
    }

    source.addEventListener("reaction_complete", () => {
      if (!voice.active) return;
      if (!voice.playing && voice.queue.length === 0 && voice.phase === "waiting") {
        if (voice.pendingTurns.length) {
          flushPendingTurns();
        } else {
          voice.currentSpeakerId = null;
          renderCallIdentity();
          resumeInputState();
        }
      }
    });

    source.addEventListener("reaction_error", event => {
      if (!voice.active) return;
      let message = "角色响应失败";
      try { message = JSON.parse(event.data || "{}").message || message; } catch (_) {}
      resumeCapture();
      setPhase("error", message);
      setTimeout(() => voice.active && resumeInputState(), 1200);
    });
  }

  async function sendTranscript(text, visualFrames = []) {
    const target = voice.target;
    if (!target) throw new Error("通话目标不存在");
    const hasVisual = Array.isArray(visualFrames) && visualFrames.length > 0;
    let response;
    if (target.scope === "group") {
      const path = hasVisual
        ? `/v1/visual/groups/${encodeURIComponent(target.conversationId)}/messages`
        : `/v1/groups/${encodeURIComponent(target.conversationId)}/messages`;
      response = await fetch(path, {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(hasVisual ? {message:text, visual_frames:visualFrames} : {message:text}),
      });
    } else {
      const path = hasVisual ? "/v1/visual/direct/messages" : "/v1/chat/messages";
      response = await fetch(path, {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
          message:text,
          character_id:target.characterId,
          conversation_id:target.conversationId,
          ...(hasVisual ? {visual_frames:visualFrames} : {}),
        }),
      });
    }
    if (!response.ok) throw new Error(await response.text());
    const result = await response.json();
    if (target.scope === "direct" && isViewingTarget() && result.message) CM.mergeDirectMessage?.(result.message);
    if (target.scope === "group" && isViewingTarget()) {
      await CM.features.groups?.reconcileLatest?.(target.conversationId);
    }
    return result;
  }

  async function dispatchRecognizedTurn(turn) {
    if (!voice.active) return;
    const text = String(turn.text || "").trim();
    if (!text) return;
    // Capture stays open while the reply is in flight. Parking it here is what made the
    // generation window deaf: the microphone collected nothing until the reaction completed,
    // and a reaction takes as long as the model takes. A turn recognised in the meantime is
    // queued by finishSpeech and sent when this one ends.
    setCapturePhase(voice.micActive ? "listening" : "idle");
    voice.turnStartedAt = performance.now();
    voice.lastMetrics = {};
    if (turn.asrMs != null) voice.lastMetrics.asr = Number(turn.asrMs);
    formatMetrics();
    if (dom.transcript) {
      dom.transcript.textContent = `你：${text}${turn.visualFrames?.length ? ` · 附 ${turn.visualFrames.length} 个视觉关键帧` : ""}`;
    }
    setPhase("waiting", "正在想…");
    try {
      const sent = await sendTranscript(text, turn.visualFrames || []);
      appendCallLog("user", text, sent.message?.id ?? sent.event_id ?? null);
    } catch (error) {
      if (voice.micActive) setCapturePhase("listening");
      else setCapturePhase("idle");
      setPhase("error", `语音失败：${error.message}`);
      setTimeout(() => voice.active && resumeInputState(), 1200);
    }
  }

  function mergedPendingTurn() {
    const turns = voice.pendingTurns.splice(0);
    if (!turns.length) return null;
    const seenFrames = new Set();
    const visualFrames = [];
    for (const turn of turns) {
      for (const frame of turn.visualFrames || []) {
        if (seenFrames.has(frame)) continue;
        seenFrames.add(frame);
        visualFrames.push(frame);
      }
    }
    const measuredAsr = turns
      .map(turn => turn.asrMs)
      .filter(value => value != null)
      .map(Number)
      .filter(Number.isFinite);
    return {
      text:turns.map(turn => String(turn.text || "").trim()).filter(Boolean).join("\n"),
      visualFrames:visualFrames.slice(-4),
      asrMs:measuredAsr.length ? measuredAsr.reduce((sum, value) => sum + value, 0) : null,
    };
  }

  // True from the moment a turn is dispatched until its reply is over. A turn recognised while
  // this holds is queued rather than sent: a second message sent into a reaction that is still
  // running is a message the person did not get to finish saying. `waiting` means exactly this
  // window -- dispatched, no audio playing yet -- so the status writers below must not overwrite
  // it while it holds, or a refused transcript mid-reply would re-open direct dispatch.
  function replyInFlight() {
    return voice.playing || voice.queue.length > 0 || voice.phase === "waiting";
  }

  // Deliberately not replyInFlight(): this is the call that ends that window, so asking whether
  // the window is still open would make it refuse its own flush.
  async function flushPendingTurns() {
    if (!voice.active || voice.playing || voice.queue.length || !voice.pendingTurns.length) return;
    const turn = mergedPendingTurn();
    if (turn) await dispatchRecognizedTurn(turn);
  }

  async function finishSpeech() {
    if (!voice.active || !voice.micActive || !voice.chunks.length) return;
    const speechEndedAt = performance.now();
    const visualFrames = voice.visualSession?.selectFrames?.({
      fromMs:Math.max(0, voice.speechStartedAt - 1000),
      toMs:speechEndedAt,
      maxFrames:4,
    }) || [];
    const chunks = voice.chunks;
    voice.chunks = [];
    voice.preRoll = [];
    voice.hotFrames = 0;
    setCapturePhase("transcribing");
    if (!replyInFlight()) setPhase("transcribing", "识别中…");
    const sourceRate = voice.audioContext.sampleRate;
    const raw = concatChunks(chunks);
    const pcm = downsample(raw, sourceRate, 16000);
    const blob = wavBlob(pcm, 16000);
    const asrStarted = performance.now();
    try {
      const response = await fetch(`${mediaBase()}/v1/asr`, {
        method: "POST",
        headers: {"Content-Type":"audio/wav", "X-ASR-Source":"call"},
        body: blob,
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      const asrMs = performance.now() - asrStarted;
      voice.lastMetrics.asr = asrMs;
      formatMetrics();

      const validation = validateAsrTranscript(result.text);
      if (!validation.valid) {
        if (voice.micActive) setCapturePhase("listening");
        else setCapturePhase("idle");
        // Reported even while the character is speaking. A refusal that shows
        // nothing reads as "it did not hear me", so the user repeats themselves
        // into a path that will refuse them again.
        if (dom.transcript) dom.transcript.textContent = "没有识别到有效内容";
        if (!replyInFlight()) {
          if (voice.micActive) setPhase("listening", "正在听…");
          else resumeInputState();
        }
        console.debug("[voice] ignored invalid ASR transcript", validation.reason, validation.text);
        return;
      }

      const turn = {text:validation.text, visualFrames, asrMs};
      if (replyInFlight()) {
        voice.pendingTurns.push(turn);
        if (voice.micActive) setCapturePhase("listening");
        else setCapturePhase("idle");
        // The character is not always mid-sentence when this shows -- the reply may still be
        // being thought about -- so the wording says what is true in both windows.
        if (dom.transcript) dom.transcript.textContent = `你：${validation.text} · 已听到，等这轮回应结束后发送…`;
        return;
      }
      await dispatchRecognizedTurn(turn);
    } catch (error) {
      if (voice.micActive) setCapturePhase("listening");
      else setCapturePhase("idle");
      if (!replyInFlight()) {
        setPhase("error", `语音失败：${error.message}`);
        setTimeout(() => voice.active && resumeInputState(), 1200);
      } else {
        // Same reason as the invalid branch: an invisible failure during the reply is
        // indistinguishable from being ignored.
        if (dom.transcript) dom.transcript.textContent = `语音失败：${error.message}`;
        console.warn("[voice] ASR failed while a reply was in flight", error);
      }
    }
  }

  async function synthesize(item) {
    const started = performance.now();
    const speakerId = stableSpeakerId(item.characterId);
    voice.speakerId = speakerId;
    const response = await fetch(`${mediaBase()}/v1/tts`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({text:item.text, voice:item.characterId, speaker_id:speakerId}),
    });
    if (!response.ok) throw new Error(await response.text());
    const blob = await response.blob();
    voice.lastMetrics.tts = performance.now() - started;
    formatMetrics();
    return URL.createObjectURL(blob);
  }

  function scheduleSynthesis(item) {
    if (item.audioPromise) return item.audioPromise;
    const run = () => synthesize(item);
    item.audioPromise = voice.ttsTail.then(run, run).then(url => {
      if (!voice.active) {
        URL.revokeObjectURL(url);
        throw new Error("voice call ended");
      }
      item.audioUrl = url;
      return url;
    });
    voice.ttsTail = item.audioPromise.catch(() => null);
    return item.audioPromise;
  }

  function prefetchNext() {
    if (!voice.active || !voice.playing || !voice.queue.length) return;
    const next = voice.queue[0];
    scheduleSynthesis(next).catch(error => {
      if (voice.active) console.warn("[voice] TTS prefetch failed", error);
    });
  }

  async function playAudio(url) {
    await new Promise((resolve, reject) => {
      const audio = new Audio(url);
      voice.currentAudio = audio;
      audio.onended = resolve;
      audio.onerror = reject;
      audio.play().catch(reject);
    });
  }

  async function playQueue() {
    if (!voice.active || voice.playing) return;
    voice.playing = true;
    let failed = null;
    try {
      while (voice.active && voice.queue.length) {
        const item = voice.queue.shift();
        voice.currentSpeakerId = item.characterId;
        renderCallIdentity();
        // Each queued item lands here, so a three-action reply used to reset capture three
        // times -- three chances to drop the sentence the person was in the middle of.
        resumeCapture();
        setPhase("speaking", `${speakerName(item.characterId)} 正在说…`);
        const url = await scheduleSynthesis(item);
        prefetchNext();
        try {
          await playAudio(url);
        } finally {
          voice.currentAudio = null;
          if (item.audioUrl) {
            URL.revokeObjectURL(item.audioUrl);
            item.audioUrl = null;
          }
        }
      }
    } catch (error) {
      failed = error;
      if (voice.active) {
        voice.currentSpeakerId = null;
        renderCallIdentity();
        resumeCapture();
        setPhase("error", `TTS 失败：${error.message}`);
      }
    } finally {
      voice.playing = false;
    }

    if (!voice.active) return;
    voice.currentSpeakerId = null;
    renderCallIdentity();
    if (failed) {
      setTimeout(() => voice.active && resumeInputState(), 1200);
      return;
    }
    voice.lastMetrics.total = voice.turnStartedAt ? performance.now() - voice.turnStartedAt + Number(voice.lastMetrics.asr || 0) : null;
    formatMetrics();
    if (voice.pendingTurns.length) {
      await flushPendingTurns();
    } else {
      resumeInputState();
    }
  }

  function audioFrame(event) {
    if (!voice.active || !voice.micActive || !["listening", "recording"].includes(voice.capturePhase)) return;
    const input = event.inputBuffer.getChannelData(0);
    const chunk = new Float32Array(input);
    const level = rms(chunk);
    const now = performance.now();

    if (voice.capturePhase === "listening") {
      voice.preRoll.push(chunk);
      while (voice.preRoll.length > 6) voice.preRoll.shift();
      if (level >= voice.threshold) voice.hotFrames += 1;
      else voice.hotFrames = 0;
      if (voice.hotFrames >= 2) {
        voice.chunks = [...voice.preRoll];
        voice.preRoll = [];
        voice.speechStartedAt = now;
        voice.lastVoiceAt = now;
        setCapturePhase("recording");
        // Speaking over a reply must not erase the fact that a reply is still coming: that is
        // the state finishSpeech reads to decide between queueing and sending.
        if (!replyInFlight()) setPhase("recording", "正在听你说…");
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

  // The permission prompt outlives the click that opened it, so a start may only adopt the
  // stream the browser hands it while it is still the newest start and nothing has stopped the
  // microphone since. A track that is still live after the call is over is hardware with no
  // control left on the page that could switch it off. Same shape as visual_capture's owner
  // ticket: every start takes a ticket, a newer start retires the one before it, and
  // stopMicrophone retires whatever is still waiting on the browser.
  let micRequestSeq = 0;
  let micOwnerSeq = 0;

  function claimMicTicket() {
    micRequestSeq += 1;
    return micRequestSeq;
  }

  const releaseStream = mediaAudio.releaseStream;

  async function stopMicrophone({updateStatus = true} = {}) {
    micOwnerSeq = claimMicTicket(); // whatever the browser is still asking permission for is void now
    voice.micActive = false;
    try { voice.asrSession?.cancel?.(); } catch (_) {}
    voice.asrSession = null;
    voice.streamingAsr = false;
    voice.streamingSegmentId = null;
    voice.streamingSegmentStartedAt = 0;
    voice.seenAsrSegments = new Set();
    await mediaAudio.closeCapture({
      stream:voice.stream,
      context:voice.audioContext,
      source:voice.sourceNode,
      processor:voice.processor,
    });
    voice.stream = null;
    voice.audioContext = null;
    voice.sourceNode = null;
    voice.processor = null;
    voice.chunks = [];
    voice.preRoll = [];
    voice.hotFrames = 0;
    setCapturePhase("idle");
    updateMicUi();
    if (updateStatus) resumeInputState();
  }

  async function startMicrophone({throwOnError = true} = {}) {
    if (!voice.active) throw new Error("请先开始通话");
    if (voice.micActive) return true;
    const ticket = claimMicTicket();
    micOwnerSeq = ticket - 1; // this start supersedes every earlier one, whichever answer lands first
    try {
      const health = await checkMedia();
      applyVoiceCapture(health);
      if (ticket <= micOwnerSeq) {
        return false;
      }
      const stream = await mediaAudio.requestMicrophone();
      if (ticket <= micOwnerSeq) {
        releaseStream(stream);
        return false;
      }

      const {context, source} = mediaAudio.createCapture(stream);
      voice.stream = stream;
      voice.audioContext = context;
      voice.sourceNode = source;
      voice.processor = null;
      voice.asrSession = null;
      voice.streamingAsr = false;

      if (canStreamAsr(health, context)) {
        try {
          await attachStreamingAsr(context, source);
        } catch (streamError) {
          if (ticket <= micOwnerSeq) throw streamError;
          console.warn("[voice] streaming ASR unavailable; falling back to batch capture", streamError);
          try { voice.asrSession?.close?.(); } catch (_) {}
          voice.asrSession = null;
          voice.streamingAsr = false;
          attachBatchAsr(context, source);
        }
      } else {
        attachBatchAsr(context, source);
      }

      if (ticket <= micOwnerSeq) {
        await stopMicrophone({updateStatus:false});
        return false;
      }

      voice.micActive = true;
      voice.preRoll = [];
      voice.chunks = [];
      voice.hotFrames = 0;
      voice.streamingSegmentId = null;
      voice.streamingSegmentStartedAt = 0;
      voice.seenAsrSegments = new Set();
      setCapturePhase("listening");
      updateMicUi();
      setPhase("listening", voice.streamingAsr ? "正在听… · 流式识别" : "正在听…");
      return true;
    } catch (error) {
      if (ticket <= micOwnerSeq) return false; // a retired start reports nothing, not even its own failure
      await stopMicrophone({updateStatus:false});
      if (dom.transcript) {
        dom.transcript.textContent = "麦克风未开启：" + error.message + "。仍可共享屏幕/摄像头，并通过聊天框发送文字。";
      }
      setPhase("muted", "麦克风未开启 · 可继续屏幕共享");
      if (throwOnError) throw error;
      return false;
    }
  }

  async function toggleMicrophone() {
    if (!voice.active) return;
    if (voice.micActive) await stopMicrophone();
    else await startMicrophone();
  }

  async function startCall() {
    if (voice.active) {
      expandCall();
      return;
    }
    dom.button.disabled = true;
    try {
      const target = captureTarget();
      voice.active = true;
      voice.micActive = false;
      voice.minimized = false;
      voice.target = target;
      voice.queue = [];
      voice.pendingTurns = [];
      voice.playing = false;
      voice.ttsTail = Promise.resolve();
      voice.currentAudio = null;
      voice.currentSpeakerId = null;
      voice.lastMetrics = {};
      voice.preRoll = [];
      voice.chunks = [];
      voice.hotFrames = 0;
      voice.asrSession = null;
      voice.streamingAsr = false;
      voice.streamingSegmentId = null;
      voice.streamingSegmentStartedAt = 0;
      voice.seenAsrSegments = new Set();
      setCapturePhase("idle");
      openVoiceEvents();

      if (dom.transcript) {
        dom.transcript.textContent = "可语音，也可关闭麦克风后只共享屏幕；共享期间在聊天框发送文字会自动附带当前关键帧。";
      }
      resetCallLog();
      renderCallIdentity();
      formatMetrics();
      updateMicUi();
      updateVisualUi();
      dom.dock?.classList.add("hidden");
      dom.overlay?.classList.remove("hidden");
      updateCallButton();
      setPhase("connecting", "正在准备通话…");
      await startMicrophone({throwOnError:false});
    } catch (error) {
      await stopCall();
      alert("无法开始通话：" + error.message);
    } finally {
      dom.button.disabled = false;
    }
  }

  async function stopCall() {
    voice.active = false;
    stopPeriodicVisualObservation();
    voice.minimized = false;
    voice.eventSource?.close?.();
    voice.eventSource = null;
    if (voice.currentAudio) {
      const audio = voice.currentAudio;
      try {
        audio.pause();
        audio.currentTime = 0;
        audio.onended?.();
      } catch (_) {}
    }
    voice.currentAudio = null;
    for (const item of voice.queue) {
      if (item.audioUrl) URL.revokeObjectURL(item.audioUrl);
    }
    await stopMicrophone({updateStatus:false});
    voice.visualSession?.stop?.({clearCandidates:true, reason:"视觉已关闭"});
    voice.visualSession = null;
    voice.periodicVisualLastFrameAt = 0;
    voice.queue = [];
    voice.pendingTurns = [];
    voice.chunks = [];
    voice.preRoll = [];
    voice.hotFrames = 0;
    voice.playing = false;
    voice.ttsTail = Promise.resolve();
    setCapturePhase("idle");
    voice.currentSpeakerId = null;
    voice.target = null;
    updateMicUi();
    updateVisualUi({active:false, source:null, candidateCount:0});
    setPhase("idle", "");
    dom.overlay?.classList.add("hidden");
    dom.dock?.classList.add("hidden");
    updateCallButton();
  }

  dom.button?.addEventListener("click", startCall);
  dom.mic?.addEventListener("click", () => toggleMicrophone().catch(error => alert("无法切换麦克风：" + error.message)));
  dom.minimize?.addEventListener("click", minimizeCall);
  dom.hangup?.addEventListener("click", stopCall);
  dom.dockExpand?.addEventListener("click", expandCall);
  dom.dockHangup?.addEventListener("click", stopCall);
  dom.camera?.addEventListener("click", () => startCameraVisual().catch(error => alert(`无法打开摄像头：${error.message}`)));
  dom.screen?.addEventListener("click", () => startScreenVisual().catch(error => {
    if (error?.name !== "NotAllowedError") alert(`无法开始屏幕共享：${error.message}`);
  }));
  dom.visualStop?.addEventListener("click", () => stopVisual({clearCandidates:false}));
  window.addEventListener("beforeunload", () => { if (voice.active) stopCall(); });
  CM.on("conversationChanged", () => { if (voice.active) renderCallIdentity(); });
  CM.registerFeature("voice", {
    start:startCall,
    stop:stopCall,
    minimize:minimizeCall,
    expand:expandCall,
    startMicrophone,
    stopMicrophone,
    toggleMicrophone,
    startCamera:startCameraVisual,
    startScreen:startScreenVisual,
    stopVisual,
    visualFramesForCurrentConversation,
    sendTextWithVisual,
    state:voice,
    stableSpeakerId,
    validateAsrTranscript,
  });
  updateCallButton();
  updateMicUi();
  updateVisualUi({active:false, source:null, candidateCount:0});
})();