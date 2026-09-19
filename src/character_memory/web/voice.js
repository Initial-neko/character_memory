(() => {
  const CM = window.CM;
  if (!CM) return;

  const MEDIA_BASE_KEY = "character-memory:media-base-url";
  const TTS_SPEAKER_IDS = [0, 2, 5];
  const SPEAKABLE_ACTIONS = new Set(["MESSAGE", "REPLY", "MINIMAL_RESPONSE", "PROACTIVE_MESSAGE"]);
  const mediaBase = () => localStorage.getItem(MEDIA_BASE_KEY) || "http://127.0.0.1:8001";

  const voice = {
    active: false,
    minimized: false,
    phase: "idle",
    capturePhase: "idle",
    target: null,
    currentSpeakerId: null,
    stream: null,
    audioContext: null,
    sourceNode: null,
    processor: null,
    eventSource: null,
    visualSession: null,
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
    silenceMs: 450,
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
      if (dom.context) dom.context.textContent = "单聊语音";
      if (dom.dockTitle) dom.dockTitle.textContent = name;
    }
  }

  function updateCallButton() {
    if (!dom.button) return;
    dom.button.classList.toggle("active", voice.active);
    dom.button.title = voice.active ? "返回正在进行的语音通话" : "语音通话";
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

  async function startCameraVisual() {
    if (!voice.active) throw new Error("请先开始通话");
    const session = ensureVisualSession();
    await session.startCamera();
    updateVisualUi();
  }

  async function startScreenVisual() {
    if (!voice.active) throw new Error("请先开始通话");
    const session = ensureVisualSession();
    await session.startDisplay();
    updateVisualUi();
  }

  function stopVisual({clearCandidates = false} = {}) {
    voice.visualSession?.stop?.({clearCandidates, reason:"视觉已关闭"});
    updateVisualUi();
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

  function validateAsrTranscript(raw) {
    const text = String(raw || "").trim();
    if (!text) return {valid:false, text:"", reason:"empty"};
    const meaningful = text.replace(/[\s\p{P}\p{S}]/gu, "");
    if (!meaningful) return {valid:false, text, reason:"punctuation_only"};
    if (/\p{Script=Han}/u.test(text)) return {valid:true, text, reason:"valid"};
    const latinOrDigitCount = (text.match(/[A-Za-z0-9]/g) || []).length;
    if (latinOrDigitCount >= 2) return {valid:true, text, reason:"valid"};
    return {valid:false, text, reason:"too_short"};
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
    setCapturePhase("listening");
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
          setCapturePhase("listening");
          setPhase("listening", "正在听…");
        }
      }
    });

    source.addEventListener("reaction_error", event => {
      if (!voice.active) return;
      let message = "角色响应失败";
      try { message = JSON.parse(event.data || "{}").message || message; } catch (_) {}
      setCapturePhase("listening");
      setPhase("error", message);
      setTimeout(() => voice.active && setPhase("listening", "正在听…"), 1200);
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
    setCapturePhase("paused");
    voice.turnStartedAt = performance.now();
    voice.lastMetrics = {asr:Number(turn.asrMs || 0)};
    formatMetrics();
    if (dom.transcript) {
      dom.transcript.textContent = `你：${text}${turn.visualFrames?.length ? ` · 附 ${turn.visualFrames.length} 个视觉关键帧` : ""}`;
    }
    setPhase("waiting", "正在想…");
    try {
      const sent = await sendTranscript(text, turn.visualFrames || []);
      appendCallLog("user", text, sent.message?.id ?? sent.event_id ?? null);
    } catch (error) {
      setCapturePhase("listening");
      setPhase("error", `语音失败：${error.message}`);
      setTimeout(() => voice.active && setPhase("listening", "正在听…"), 1200);
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
    return {
      text:turns.map(turn => String(turn.text || "").trim()).filter(Boolean).join("\n"),
      visualFrames:visualFrames.slice(-4),
      asrMs:turns.reduce((sum, turn) => sum + Number(turn.asrMs || 0), 0),
    };
  }

  async function flushPendingTurns() {
    if (!voice.active || voice.playing || voice.queue.length || !voice.pendingTurns.length) return;
    const turn = mergedPendingTurn();
    if (turn) await dispatchRecognizedTurn(turn);
  }

  async function finishSpeech() {
    if (!voice.active || !voice.chunks.length) return;
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
    if (!voice.playing) setPhase("transcribing", "识别中…");
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
      const asrMs = performance.now() - asrStarted;
      voice.lastMetrics.asr = asrMs;
      formatMetrics();

      const validation = validateAsrTranscript(result.text);
      if (!validation.valid) {
        setCapturePhase("listening");
        if (dom.transcript && !voice.playing) dom.transcript.textContent = "没有识别到有效内容";
        if (!voice.playing) setPhase("listening", "正在听…");
        console.debug("[voice] ignored invalid ASR transcript", validation.reason, validation.text);
        return;
      }

      const turn = {text:validation.text, visualFrames, asrMs};
      if (voice.playing || voice.queue.length) {
        voice.pendingTurns.push(turn);
        setCapturePhase("listening");
        if (dom.transcript) dom.transcript.textContent = `你：${validation.text} · 已听到，等待对方说完…`;
        return;
      }
      await dispatchRecognizedTurn(turn);
    } catch (error) {
      setCapturePhase("listening");
      if (!voice.playing) {
        setPhase("error", `语音失败：${error.message}`);
        setTimeout(() => voice.active && setPhase("listening", "正在听…"), 1200);
      } else {
        console.warn("[voice] ASR failed during playback", error);
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
        setCapturePhase("listening");
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
        setCapturePhase("listening");
        setPhase("error", `TTS 失败：${error.message}`);
      }
    } finally {
      voice.playing = false;
    }

    if (!voice.active) return;
    voice.currentSpeakerId = null;
    renderCallIdentity();
    if (failed) {
      setTimeout(() => voice.active && setPhase("listening", "正在听…"), 1200);
      return;
    }
    voice.lastMetrics.total = voice.turnStartedAt ? performance.now() - voice.turnStartedAt + Number(voice.lastMetrics.asr || 0) : null;
    formatMetrics();
    if (voice.pendingTurns.length) {
      await flushPendingTurns();
    } else {
      setCapturePhase("listening");
      setPhase("listening", "正在听…");
    }
  }

  function audioFrame(event) {
    if (!voice.active || !["listening", "recording"].includes(voice.capturePhase)) return;
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
        if (!voice.playing) setPhase("recording", "正在听你说…");
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
    if (voice.active) {
      expandCall();
      return;
    }
    dom.button.disabled = true;
    try {
      const target = captureTarget();
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
      voice.minimized = false;
      voice.target = target;
      voice.stream = stream;
      voice.audioContext = context;
      voice.sourceNode = source;
      voice.processor = processor;
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
      setCapturePhase("listening");
      ensureVisualSession();
      openVoiceEvents();

      if (dom.transcript) dom.transcript.textContent = "直接说话即可；AI 说话时也会继续听，但不会打断当前语音。摄像头/屏幕开启后只会抽取少量关键帧。";
      resetCallLog();
      renderCallIdentity();
      formatMetrics();
      updateVisualUi();
      dom.dock?.classList.add("hidden");
      dom.overlay?.classList.remove("hidden");
      updateCallButton();
      setPhase("listening", "正在听…");
    } catch (error) {
      alert(`无法开始语音：${error.message}`);
    } finally {
      dom.button.disabled = false;
    }
  }

  async function stopCall() {
    voice.active = false;
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
    voice.processor?.disconnect?.();
    voice.sourceNode?.disconnect?.();
    voice.stream?.getTracks?.().forEach(track => track.stop());
    voice.visualSession?.stop?.({clearCandidates:true, reason:"视觉已关闭"});
    voice.visualSession = null;
    try { await voice.audioContext?.close?.(); } catch (_) {}
    voice.stream = null;
    voice.audioContext = null;
    voice.sourceNode = null;
    voice.processor = null;
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
    updateVisualUi({active:false, source:null, candidateCount:0});
    setPhase("idle", "");
    dom.overlay?.classList.add("hidden");
    dom.dock?.classList.add("hidden");
    updateCallButton();
  }

  dom.button?.addEventListener("click", startCall);
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
    startCamera:startCameraVisual,
    startScreen:startScreenVisual,
    stopVisual,
    state:voice,
    stableSpeakerId,
    validateAsrTranscript,
  });
  updateCallButton();
  updateVisualUi({active:false, source:null, candidateCount:0});
})();