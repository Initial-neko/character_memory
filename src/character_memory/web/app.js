(() => {
  const activeCharacterKey = "character-memory:active-character";
  const hiddenText = "╭(╯^╰)╮不给看";

  const CM = window.CM = {
    state: {
      characterId: localStorage.getItem(activeCharacterKey) || "rin",
      characters: [],
      pendingCharacters: new Set(),
      lastRenderedSignature: "",
      conversation: {type: "DIRECT", groupId: null},
      directHistory: {messages: [], hasMore: false, nextBeforeId: null, loadingOlder: false},
      directStream: null,
      directStreamKey: null,
      runtimeMemories: [],
      characterCapacity: {activeTotal:0, softLimit:10, hardLimit:20},
    },
    features: {},
    listeners: new Map(),
  };

  CM.dom = {
    chat: document.getElementById("chat"),
    composer: document.getElementById("composer"),
    input: document.getElementById("messageInput"),
    sendButton: document.getElementById("sendButton"),
    runtimeButton: document.getElementById("runtimeButton"),
    nowText: document.getElementById("nowText"),
    characterList: document.getElementById("characterList"),
    characterName: document.getElementById("characterName"),
    characterIdentity: document.getElementById("characterIdentity"),
    headerAvatar: document.getElementById("headerAvatar"),
    drawer: document.getElementById("drawer"),
    drawerBackdrop: document.getElementById("drawerBackdrop"),
    drawerClose: document.getElementById("drawerClose"),
    drawerTitle: document.getElementById("drawerTitle"),
    drawerSubtitle: document.getElementById("drawerSubtitle"),
    drawerBody: document.getElementById("drawerBody"),
    typingTemplate: document.getElementById("typingTemplate"),
  };

  CM.escapeHtml = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  CM.fmtTime = iso => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"}); };
  CM.fmtDate = iso => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(); };
  CM.fmtMs = value => { const n = Number(value || 0); return n >= 1000 ? `${(n / 1000).toFixed(2)}s` : `${n.toFixed(0)}ms`; };
  CM.hiddenIfEmpty = value => String(value ?? "").trim() || hiddenText;
  CM.initialFor = profile => (profile?.name || profile?.id || "AI").trim().slice(0, 1).toUpperCase();
  CM.currentProfile = () => CM.state.characters.find(item => item.id === CM.state.characterId) || {id: CM.state.characterId, name: CM.state.characterId, identity:"", tagline:""};
  CM.isGroupConversation = () => CM.state.conversation.type === "GROUP" && Boolean(CM.state.conversation.groupId);
  CM.registerFeature = (name, feature) => { CM.features[name] = feature; return feature; };
  CM.on = (name, fn) => { if (!CM.listeners.has(name)) CM.listeners.set(name, []); CM.listeners.get(name).push(fn); };
  CM.emit = async (name, payload) => { for (const fn of CM.listeners.get(name) || []) await fn(payload); };

  CM.conversationIdFor = id => {
    const key = `character-memory:conversation:${id}`;
    let value = localStorage.getItem(key);
    if (!value) { value = crypto.randomUUID(); localStorage.setItem(key, value); }
    return value;
  };

  CM.api = async (path, options = {}) => {
    const started = performance.now();
    const response = await fetch(path, {headers:{"Content-Type":"application/json", ...(options.headers || {})}, ...options});
    const elapsed = performance.now() - started;
    if (!response.ok) {
      const text = await response.text();
      console.error("[api]", path, response.status, `${elapsed.toFixed(1)}ms`, text);
      throw new Error(`${response.status} ${response.statusText}: ${text}`);
    }
    const data = await response.json();
    console.info("[api]", path, `${elapsed.toFixed(1)}ms`);
    return data;
  };

  CM.openDrawer = (title, subtitle = "") => {
    const d = CM.dom;
    d.drawerTitle.textContent = title;
    d.drawerSubtitle.textContent = subtitle;
    d.drawerBackdrop.classList.remove("hidden");
    d.drawer.classList.add("open");
    d.drawer.setAttribute("aria-hidden", "false");
  };
  CM.closeDrawer = () => {
    const d = CM.dom;
    d.drawer.classList.remove("open");
    d.drawer.setAttribute("aria-hidden", "true");
    setTimeout(() => d.drawerBackdrop.classList.add("hidden"), 180);
  };

  CM.voicePlayer = {audio:null, button:null, messageId:null};

  // Voice markup is rendered by message_content.js, which is not optional: this
  // file calls CM.messageContentHtml on every message and owns only the shared
  // audio player and the click binding below.

  CM.stopVoiceMessage = () => {
    const current = CM.voicePlayer;
    if (current.audio) {
      current.audio.pause();
      current.audio.currentTime = 0;
    }
    current.button?.classList.remove("playing");
    CM.voicePlayer = {audio:null, button:null, messageId:null};
  };

  CM.bindVoiceMessage = row => {
    row.querySelector("[data-voice-play]")?.addEventListener("click", event => {
      const button = event.currentTarget;
      const messageId = row.dataset.messageId || "";
      if (CM.voicePlayer.messageId === messageId && CM.voicePlayer.audio) {
        if (CM.voicePlayer.audio.paused) {
          CM.voicePlayer.audio.play().catch(console.warn);
          button.classList.add("playing");
        } else {
          CM.voicePlayer.audio.pause();
          button.classList.remove("playing");
        }
        return;
      }
      CM.stopVoiceMessage();
      const audio = new Audio(button.dataset.audioUrl);
      CM.voicePlayer = {audio, button, messageId};
      button.classList.add("playing");
      audio.addEventListener("ended", () => CM.stopVoiceMessage(), {once:true});
      audio.addEventListener("error", () => { button.classList.remove("playing"); button.classList.add("broken"); }, {once:true});
      audio.play().catch(error => { button.classList.remove("playing"); console.warn("voice message playback failed", error); });
    });
  };

  CM.addMessage = message => {
    const d = CM.dom;
    const row = document.createElement("article");
    row.className = `message-row ${message.role}`;
    row.dataset.messageId = message.id ?? "";
    const avatar = message.role === "assistant" ? CM.initialFor(CM.currentProfile()) : "";
    const thought = message.role === "assistant" && message.has_trace && message.source_event_id
      ? `<button class="detail-button" type="button" data-thought="${message.source_event_id}" title="查看安全的思考摘要">想法</button>` : "";
    const trace = message.has_trace && message.source_event_id
      ? `<button class="detail-button trace-button" type="button" data-trace="${message.source_event_id}" title="查看本轮 LLM 实际输入与原始响应">LLM</button>` : "";
    const contentHtml = CM.messageContentHtml(message);
    const proactive = message.proactive || message.action === "PROACTIVE_MESSAGE";
    row.innerHTML = `<div class="avatar">${CM.escapeHtml(avatar)}</div><div class="bubble-wrap">${contentHtml}<div class="message-meta"><span>${CM.fmtTime(message.event_time)}</span>${proactive ? '<span class="proactive-badge">主动消息</span>' : ""}${thought}${trace}</div></div>`;
    CM.bindMessageContent(row);
    d.chat.appendChild(row);
    return row;
  };

  CM.renderHistory = (messages, {preserveScroll = false} = {}) => {
    const d = CM.dom;
    const beforeHeight = document.body.scrollHeight;
    const beforeY = window.scrollY;
    const signature = `${CM.state.characterId}|${CM.state.directHistory.hasMore}|${CM.state.pendingCharacters.has(CM.state.characterId)}|` + messages.map(m => `${m.id}:${m.event_time}:${m.content}:${m.sticker_id || ""}:${m.image_id || m.media_id || ""}:${m.voice_status || ""}:${m.voice_media_id || ""}`).join("|");
    if (signature === CM.state.lastRenderedSignature && d.chat.children.length) return;
    d.chat.innerHTML = "";
    if (!messages.length) {
      d.chat.innerHTML = `<div class="empty">还没有和 ${CM.escapeHtml(CM.currentProfile().name || CM.state.characterId)} 的聊天记录。<br>从第一句话开始认识彼此。</div>`;
      CM.state.lastRenderedSignature = signature;
      if (CM.state.pendingCharacters.has(CM.state.characterId)) CM.appendTypingForCurrent();
      return;
    }
    if (CM.state.directHistory.hasMore) {
      const older = document.createElement("div");
      older.className = "date-separator history-load-more";
      older.innerHTML = `<button class="detail-button" type="button" data-load-older-direct ${CM.state.directHistory.loadingOlder ? "disabled" : ""}>${CM.state.directHistory.loadingOlder ? "正在加载…" : "加载更早的消息"}</button>`;
      d.chat.appendChild(older);
    }
    let lastDate = null;
    for (const message of messages) {
      const date = CM.fmtDate(message.event_time);
      if (date !== lastDate) {
        const sep = document.createElement("div");
        sep.className = "date-separator";
        sep.textContent = `── ${date} ──`;
        d.chat.appendChild(sep);
        lastDate = date;
      }
      CM.addMessage(message);
    }
    if (CM.state.pendingCharacters.has(CM.state.characterId)) CM.appendTypingForCurrent();
    CM.state.lastRenderedSignature = signature;
    if (preserveScroll) {
      requestAnimationFrame(() => {
        const delta = document.body.scrollHeight - beforeHeight;
        window.scrollTo({top: beforeY + delta, behavior:"auto"});
      });
    } else {
      CM.scrollToBottom(false);
    }
  };

  CM.appendTypingForCurrent = () => {
    if (CM.dom.chat.querySelector(".typing-row")) return;
    const typing = CM.dom.typingTemplate.content.cloneNode(true);
    typing.querySelector(".avatar").textContent = CM.initialFor(CM.currentProfile());
    CM.dom.chat.appendChild(typing);
  };
  CM.scrollToBottom = (smooth = true) => window.scrollTo({top: document.body.scrollHeight, behavior: smooth ? "smooth" : "auto"});

  CM.renderCharacterList = () => {
    const unread = CM.features.unread;
    const all = CM.state.characters || [];
    let visible = all.slice(0, 10);
    if (!CM.isGroupConversation() && CM.state.characterId && !visible.some(item => item.id === CM.state.characterId)) {
      const activeProfile = all.find(item => item.id === CM.state.characterId);
      if (activeProfile) visible = [...visible.slice(0, 9), activeProfile];
    }
    const rows = visible.map(profile => {
      const pending = CM.state.pendingCharacters.has(profile.id);
      const hasUnread = unread?.isUnread?.(profile.id) || false;
      const preview = unread?.preview?.(profile) || profile.tagline || profile.identity || "Persistent AI Person";
      const active = !CM.isGroupConversation() && profile.id === CM.state.characterId;
      const id = CM.escapeHtml(profile.id);
      return `<div class="character-item-wrap" data-character-row="${id}"><button class="character-item ${active ? "active" : ""}" type="button" data-character="${id}"><span class="character-avatar">${CM.escapeHtml(CM.initialFor(profile))}</span><span class="character-copy"><span class="character-name character-name-line"><span>${CM.escapeHtml(profile.name)}${pending ? '<span class="character-pending"> · 输入中</span>' : ""}</span>${hasUnread ? '<span class="unread-dot" title="有新消息" aria-label="有新消息"></span>' : ""}</span><span class="character-tagline character-preview">${CM.escapeHtml(preview)}</span></span></button><button class="character-more-button" type="button" data-character-more="${id}" title="人物操作" aria-label="人物操作">···</button><div class="character-context-menu hidden" data-character-menu="${id}"><button type="button" data-character-persona="${id}">查看人物设定</button><button type="button" data-character-archive="${id}">归档人物…</button></div></div>`;
    }).join("");
    const hiddenCount = Math.max(0, all.length - visible.length);
    const overflow = hiddenCount
      ? `<button class="character-overflow-entry" type="button" data-character-overflow>查看更多人物（${hiddenCount}）</button>`
      : "";
    CM.dom.characterList.innerHTML = rows + overflow;
  };

  CM.showCharacterOverflow = () => {
    const current = CM.state.characterId;
    CM.openDrawer("全部人物", `当前 ${CM.state.characterCapacity.activeTotal || CM.state.characters.length} / ${CM.state.characterCapacity.hardLimit || 20} 位；侧边栏只展示最多 10 位`);
    CM.dom.drawerBody.innerHTML = `<div class="character-overflow-list">${CM.state.characters.map(profile => {
      const active = !CM.isGroupConversation() && profile.id === current;
      return `<button type="button" class="character-overflow-card ${active ? "active" : ""}" data-overflow-character="${CM.escapeHtml(profile.id)}"><span class="character-avatar">${CM.escapeHtml(CM.initialFor(profile))}</span><span><strong>${CM.escapeHtml(profile.name || profile.id)}</strong><small>${CM.escapeHtml(profile.identity || profile.tagline || "")}</small></span></button>`;
    }).join("")}</div>`;
  };

  CM.updateComposerState = () => {
    if (CM.isGroupConversation() && CM.features.groups?.applyComposerState?.()) return;
    CM.dom.sendButton.disabled = false;
    CM.dom.input.disabled = false;
    CM.features.stickers?.setDisabled?.(false);
    CM.features.images?.setDisabled?.(false);
    CM.dom.input.focus();
  };

  CM.updateHeader = () => {
    if (CM.isGroupConversation() && CM.features.groups?.applyHeader?.()) { CM.updateComposerState(); return; }
    const profile = CM.currentProfile();
    const pending = CM.state.pendingCharacters.has(CM.state.characterId);
    CM.dom.characterName.textContent = profile.name || profile.id;
    CM.dom.characterIdentity.textContent = pending ? `${profile.identity || profile.tagline || "Persistent AI Person"} · 正在输入` : (profile.identity || profile.tagline || "Persistent AI Person");
    CM.dom.headerAvatar.textContent = CM.initialFor(profile);
    CM.dom.input.placeholder = `给 ${profile.name || profile.id} 发消息`;
    CM.dom.runtimeButton.disabled = false;
    CM.dom.runtimeButton.title = "";
    CM.features.intent?.setDisabled?.(false);
    CM.updateComposerState();
  };

  CM.loadCharacters = async () => {
    const data = await CM.api("/v1/characters");
    CM.state.characters = data.characters || [];
    CM.state.characterCapacity = {
      activeTotal:Number(data.active_total ?? CM.state.characters.length),
      softLimit:Number(data.soft_limit ?? 10),
      hardLimit:Number(data.active_limit ?? 20),
    };
    if (!CM.state.characters.length) throw new Error("没有发现任何 Persona");
    if (!CM.state.characters.some(item => item.id === CM.state.characterId)) CM.state.characterId = CM.state.characters[0].id;
    localStorage.setItem(activeCharacterKey, CM.state.characterId);
    CM.renderCharacterList();
    CM.updateHeader();
    await CM.emit("charactersLoaded", CM.state.characters);
  };

  CM.mergeDirectMessage = message => {
    if (!message || message.id == null) return;
    const index = CM.state.directHistory.messages.findIndex(item => item.id === message.id);
    if (index >= 0) CM.state.directHistory.messages[index] = {...CM.state.directHistory.messages[index], ...message};
    else CM.state.directHistory.messages.push(message);
    CM.state.directHistory.messages.sort((a, b) => Number(a.id || 0) - Number(b.id || 0));
    CM.state.lastRenderedSignature = "";
    CM.renderHistory(CM.state.directHistory.messages);
  };

  CM.directEventToMessage = raw => {
    const metadata = raw.metadata || {};
    const stickerId = metadata.sticker_id || null;
    const imageId = metadata.image_id || null;
    return {
      id: raw.id,
      role:"assistant",
      character_id:raw.character_id,
      content:raw.content || "",
      event_time:raw.event_time,
      action:metadata.action,
      sticker_id:stickerId,
      sticker:stickerId ? {id:stickerId,label:metadata.sticker_label || "表情包",url:`/v1/stickers/${encodeURIComponent(stickerId)}/asset`} : null,
      image_id:imageId,
      image:imageId ? {id:imageId,label:metadata.image_label || "图片",url:`/v1/images/${encodeURIComponent(raw.character_id)}/${encodeURIComponent(imageId)}/asset`} : null,
      voice_status:metadata.voice_status || null,
      voice_media_id:metadata.voice_media_id || null,
      voice_duration_ms:metadata.voice_duration_ms ?? null,
      voice_error:metadata.voice_error || null,
      source_event_type:metadata.source_event_type,
      source_event_id:metadata.source_event_id,
      has_trace:Boolean(metadata.source_event_id),
    };
  };

  CM.closeDirectStream = () => {
    CM.state.directStream?.close?.();
    CM.state.directStream = null;
    CM.state.directStreamKey = null;
  };

  CM.connectDirectStream = () => {
    if (CM.isGroupConversation()) return;
    const characterId = CM.state.characterId;
    const conversationId = CM.conversationIdFor(characterId);
    const streamKey = `${characterId}:${conversationId}`;
    if (CM.state.directStream && CM.state.directStreamKey === streamKey) return;
    CM.closeDirectStream();
    const params = new URLSearchParams({scope:"direct", character_id:characterId, conversation_id:conversationId});
    const source = new EventSource(`/v1/events/stream?${params.toString()}`);
    CM.state.directStream = source;
    CM.state.directStreamKey = streamKey;
    source.addEventListener("reaction_status", event => {
      if (CM.isGroupConversation() || characterId !== CM.state.characterId) return;
      const data = JSON.parse(event.data || "{}");
      if (["queued", "typing", "superseded"].includes(data.state)) CM.state.pendingCharacters.add(characterId);
      if (data.state === "idle") CM.state.pendingCharacters.delete(characterId);
      CM.renderCharacterList();
      CM.updateHeader();
      CM.state.lastRenderedSignature = "";
      CM.renderHistory(CM.state.directHistory.messages);
    });
    source.addEventListener("character_event", event => {
      if (CM.isGroupConversation() || characterId !== CM.state.characterId) return;
      const message = CM.directEventToMessage(JSON.parse(event.data));
      CM.mergeDirectMessage(message);
      CM.features.unread?.markRead?.(characterId);
    });
    source.addEventListener("reaction_error", event => {
      if (CM.isGroupConversation() || characterId !== CM.state.characterId) return;
      const data = JSON.parse(event.data || "{}");
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `生成失败：${data.message || "未知错误"}`;
      CM.dom.chat.appendChild(box);
    });
  };

  CM.loadDirectHistory = async ({beforeId = null, appendOlder = false} = {}) => {
    const requested = CM.state.characterId;
    const params = new URLSearchParams({character_id:requested, limit:"50"});
    if (beforeId != null) params.set("before_id", String(beforeId));
    const data = await CM.api(`/v1/chat/history-page?${params.toString()}`);
    if (CM.isGroupConversation() || requested !== CM.state.characterId) return;
    const incoming = data.messages || [];
    if (appendOlder) {
      const existing = new Set(CM.state.directHistory.messages.map(item => item.id));
      CM.state.directHistory.messages = [...incoming.filter(item => !existing.has(item.id)), ...CM.state.directHistory.messages];
    } else CM.state.directHistory.messages = incoming;
    CM.state.directHistory.hasMore = Boolean(data.has_more);
    CM.state.directHistory.nextBeforeId = data.next_before_id ?? null;
    CM.renderHistory(CM.state.directHistory.messages, {preserveScroll:appendOlder});
    CM.features.unread?.markRead?.(requested);
    CM.renderCharacterList();
    await CM.emit("historyLoaded", {type:"DIRECT", characterId:requested, messages:CM.state.directHistory.messages});
  };

  CM.loadOlderDirectHistory = async () => {
    const history = CM.state.directHistory;
    if (CM.isGroupConversation() || history.loadingOlder || !history.hasMore || history.nextBeforeId == null) return;
    history.loadingOlder = true;
    CM.state.lastRenderedSignature = "";
    CM.renderHistory(history.messages, {preserveScroll:true});
    try { await CM.loadDirectHistory({beforeId:history.nextBeforeId, appendOlder:true}); }
    finally { history.loadingOlder = false; CM.state.lastRenderedSignature = ""; CM.renderHistory(history.messages, {preserveScroll:true}); }
  };

  CM.loadHistory = async () => CM.isGroupConversation() ? CM.features.groups?.loadHistory?.() : CM.loadDirectHistory();

  CM.switchCharacter = async nextId => {
    const wasGroup = CM.isGroupConversation();
    CM.features.groups?.leave?.();
    if (!wasGroup && nextId === CM.state.characterId) return;
    CM.closeDirectStream();
    CM.features.stickers?.close?.();
    CM.features.images?.close?.();
    CM.features.unread?.markRead?.(nextId);
    CM.state.characterId = nextId;
    CM.state.directHistory = {messages: [], hasMore: false, nextBeforeId: null, loadingOlder: false};
    localStorage.setItem(activeCharacterKey, nextId);
    CM.state.lastRenderedSignature = "";
    CM.closeDrawer();
    CM.updateHeader();
    CM.renderCharacterList();
    CM.dom.chat.innerHTML = '<div class="empty">正在加载聊天记录…</div>';
    await CM.loadDirectHistory();
    CM.connectDirectStream();
    CM.updateComposerState();
    await CM.emit("conversationChanged", {type:"DIRECT", characterId:nextId});
  };

  CM.sendDirectPayload = async payload => {
    const sentCharacter = CM.state.characterId;
    const conversationId = CM.conversationIdFor(sentCharacter);
    CM.connectDirectStream();
    const result = await CM.api("/v1/chat/messages", {method:"POST", body:JSON.stringify({character_id:sentCharacter, conversation_id:conversationId, ...payload})});
    if (!CM.isGroupConversation() && sentCharacter === CM.state.characterId) CM.mergeDirectMessage(result.message);
    return result;
  };

  CM.sendDirectText = async message => {
    if (!message) return;
    CM.dom.input.value = "";
    CM.dom.input.style.height = "auto";
    try {
      const visual = await CM.features.voice?.sendTextWithVisual?.(message);
      if (visual?.handled) return visual.result;
      await CM.sendDirectPayload({message});
    }
    catch (error) {
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `发送失败：${error.message}`;
      CM.dom.chat.appendChild(box);
    }
  };

  CM.submitCurrentText = async () => {
    const message = CM.dom.input.value.trim();
    if (!message) return;
    if (CM.isGroupConversation()) return CM.features.groups?.sendText?.(message);
    return CM.sendDirectText(message);
  };

  CM.timingHtml = timings => {
    const entries = Object.entries(timings || {});
    if (!entries.length) return "<p>暂无 timing 数据。</p>";
    return `<div class="kv">${entries.map(([key, value]) => `<div>${CM.escapeHtml(key)}</div><div>${CM.escapeHtml(CM.fmtMs(value))}</div>`).join("")}</div>`;
  };

  CM.showThought = async sourceEventId => {
    CM.openDrawer(`${CM.currentProfile().name} · 想法`, "安全摘要，不是隐藏思维链");
    CM.dom.drawerBody.innerHTML = "<p>正在回想这一轮…</p>";
    try {
      const trace = await CM.api(`/v1/traces/${sourceEventId}`);
      CM.dom.drawerBody.innerHTML = `<section class="section"><h3>她/他注意到了什么</h3><p>${CM.escapeHtml(CM.hiddenIfEmpty(trace.perception))}</p></section><section class="section"><h3>这一刻的反应</h3><p>${CM.escapeHtml(CM.hiddenIfEmpty(trace.reaction))}</p></section><p class="muted">这里只展示开发者安全摘要，不展示模型隐藏推理过程。</p>`;
    } catch (error) { CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`; }
  };

  CM.traceMessagesHtml = messages => {
    const items = Array.isArray(messages) ? messages : [];
    if (!items.length) return '<p class="muted">本轮没有记录模型 messages。</p>';
    return items.map((message, index) => `
      <article class="trace-message">
        <div class="trace-message-role">${index + 1}. ${CM.escapeHtml(message.role || "unknown")}</div>
        <pre>${CM.escapeHtml(message.content || "")}</pre>
      </article>`).join("");
  };

  CM.showTrace = async sourceEventId => {
    CM.openDrawer(`${CM.currentProfile().name} · LLM 调试`, `source_event_id = ${sourceEventId} · 展示应用实际保存的模型 I/O，不展示隐藏推理过程`);
    CM.dom.drawerBody.innerHTML = "<p>正在加载本轮 LLM 记录…</p>";
    try {
      const trace = await CM.api(`/v1/traces/${sourceEventId}`);
      const actions = Array.isArray(trace.actions) ? trace.actions : (trace.action ? [trace.action] : []);
      const actionText = actions.length
        ? actions.map((action, index) => `${index + 1}. ${action.type}: ${action.message || action.sticker_id || action.image_id || action.reason || ""}`).join("\n")
        : "没有发送消息";
      const firstAction = trace.action || actions[0] || {};
      const recalled = Array.isArray(trace.recalled_memories) ? trace.recalled_memories : [];
      const memoryCandidates = Array.isArray(trace.memory_candidates) ? trace.memory_candidates : [];
      const intents = Array.isArray(trace.intent_candidates) ? trace.intent_candidates : [];
      const totalMs = trace.timings?.runtime_total_ms ?? trace.timings?.model_ms ?? "—";
      CM.dom.drawerBody.innerHTML = `
        <section class="trace-summary-card">
          <div><span>Model</span><strong>${CM.escapeHtml(trace.model_used || "—")}</strong></div>
          <div><span>Attempt</span><strong>${CM.escapeHtml(trace.model_attempt || "—")}</strong></div>
          <div><span>Actions</span><strong>${CM.escapeHtml(actions.map(item => item.type).join(" / ") || "NO_REPLY")}</strong></div>
          <div><span>Total</span><strong>${CM.escapeHtml(Number.isFinite(Number(totalMs)) ? CM.fmtMs(totalMs) : "—")}</strong></div>
          <div class="trace-call-id"><span>Logical Call ID</span><code>${CM.escapeHtml(trace.llm_logical_call_id || "—")}</code></div>
        </section>

        <section class="section trace-output-section">
          <h3>最终对外表达</h3>
          <pre>${CM.escapeHtml(actionText)}</pre>
        </section>

        <details class="inspector-details trace-io">
          <summary>模型真实输入 / 原始响应</summary>
          <p class="memory-help">这里展示 Runtime 实际保存并发送给 Provider 的 messages，以及 Provider 返回的原始结构化文本。可用 Logical Call ID 与 Dev 的 LLM Usage 对照。</p>
          <h4>发送给模型的 messages</h4>
          <div class="trace-message-list">${CM.traceMessagesHtml(trace.model_messages)}</div>
          <h3 class="debug-subheading">Raw Model Response</h3>
          <details class="debug-output">
            <summary>查看 Provider 原始响应</summary>
            <pre class="trace-raw-response">${CM.escapeHtml(CM.hiddenIfEmpty(trace.raw_model_response))}</pre>
          </details>
          <h4>Compiled Context</h4>
          <details class="debug-output">
            <summary>查看编译后的上下文</summary>
            <pre>${CM.escapeHtml(CM.hiddenIfEmpty(trace.context))}</pre>
          </details>
        </details>

        <details class="inspector-details">
          <summary>决策摘要 · ${CM.escapeHtml(actions.map(item => item.type).join(" / ") || "NO_REPLY")}</summary>
          <div class="kv">
            <div>Action Reason</div><div>${CM.escapeHtml(CM.hiddenIfEmpty(firstAction.reason))}</div>
            <div>Perception</div><div>${CM.escapeHtml(CM.hiddenIfEmpty(trace.perception))}</div>
            <div>Reaction</div><div>${CM.escapeHtml(CM.hiddenIfEmpty(trace.reaction))}</div>
          </div>
        </details>

        <details class="inspector-details">
          <summary>Recall / Memory · ${recalled.length} recalled · ${memoryCandidates.length} candidates</summary>
          <div class="card-list">${recalled.map(memory => `<div class="card"><strong>${CM.escapeHtml(memory.memory_type)}</strong><span> · importance ${CM.escapeHtml(memory.importance)}</span><div>${CM.escapeHtml(memory.content)}</div></div>`).join("") || "<p>本轮没有 Recall 到 Memory。</p>"}</div>
          <h3 class="debug-subheading">Memory Admission</h3>
          <details class="debug-output"><summary>查看 Admission JSON</summary><pre>${CM.escapeHtml(JSON.stringify(trace.memory_decisions || [], null, 2))}</pre></details>
          <h3 class="debug-subheading">Memory Write</h3>
          <details class="debug-output"><summary>查看候选与写入 ID</summary><pre>${CM.escapeHtml(JSON.stringify({
            candidates: memoryCandidates,
            created_memory_ids: trace.created_memory_ids || [],
          }, null, 2))}</pre></details>
        </details>

        <details class="inspector-details">
          <summary>Intent / Task · ${intents.length} candidates</summary>
          <h3 class="debug-subheading">Intent</h3>
          <details class="debug-output"><summary>查看 Intent JSON</summary><pre>${CM.escapeHtml(JSON.stringify({candidates:intents, created_intent_ids:trace.created_intent_ids || []}, null, 2))}</pre></details>
        </details>

        <details class="inspector-details">
          <summary>Mental State · Before / After</summary>
          <h4>Before</h4><pre>${CM.escapeHtml(CM.hiddenIfEmpty(trace.mental_state_before))}</pre>
          <h4>After</h4><pre>${CM.escapeHtml(CM.hiddenIfEmpty(trace.mental_state_after))}</pre>
        </details>

        <details class="inspector-details">
          <summary>其他执行细节 · Timing / Sticker / Channel</summary>
          <h4>耗时</h4>${CM.timingHtml(trace.timings)}
          <h3 class="debug-subheading">Sticker Retrieval</h3>
          <details class="debug-output"><summary>查看 Sticker Retrieval JSON</summary><pre>${CM.escapeHtml(JSON.stringify(trace.sticker_retrieval || {}, null, 2))}</pre></details>
          <h4>Channel Decisions</h4>
          <details class="debug-output"><summary>查看 Channel Decisions JSON</summary><pre>${CM.escapeHtml(JSON.stringify(trace.channel_decisions || [], null, 2))}</pre></details>
        </details>`;
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  };

  CM.memorySourceText = memory => {
    const source = memory.source || {};
    if (source.kind === "GROUP") return `群聊 · ${source.conversation_id || "unknown"} · #${source.event_id || "?"}`;
    if (source.event_id) return `${source.kind || source.event_type || "Event"} · #${source.event_id}`;
    const origin = memory.metadata?.origin;
    return origin ? String(origin) : "来源未记录";
  };

  CM.memoryCardHtml = memory => {
    const inactive = !memory.active;
    const superseded = Boolean(memory.superseded_by);
    const status = superseded ? `已被 #${memory.superseded_by} 纠正` : inactive ? "已忘记" : memory.pinned ? "已固定" : "普通";
    const sourceContent = memory.source?.content ? `<div class="memory-source-content">${CM.escapeHtml(memory.source.content)}</div>` : "";
    const actions = superseded
      ? ""
      : inactive
        ? `<button type="button" data-memory-action="restore" data-memory-id="${memory.id}">恢复</button>`
        : `<button type="button" data-memory-action="pin" data-memory-id="${memory.id}">${memory.pinned ? "取消固定" : "📌 固定"}</button><button type="button" data-memory-action="correct" data-memory-id="${memory.id}">纠正</button><button type="button" data-memory-action="forget" data-memory-id="${memory.id}">忘记</button>`;
    const preview = String(memory.content || "").trim().replace(/\s+/g, " ");
    const shortPreview = preview.length > 96 ? `${preview.slice(0, 96)}…` : preview;
    return `<details class="memory-card ${inactive ? "inactive" : ""}">
      <summary class="memory-card-summary">
        <span class="memory-card-head"><span><strong>${CM.escapeHtml(memory.memory_type)}</strong><span class="memory-status">${CM.escapeHtml(status)}</span></span><span>#${memory.id} · importance ${Number(memory.importance || 0).toFixed(2)}</span></span>
        <span class="memory-card-preview">${CM.escapeHtml(shortPreview || "无内容")}</span>
      </summary>
      <div class="memory-card-body">
        <div class="memory-content">${CM.escapeHtml(memory.content)}</div>
        <div class="memory-source"><span>${CM.escapeHtml(CM.memorySourceText(memory))}</span><span>${CM.escapeHtml(CM.fmtDate(memory.event_time))} ${CM.escapeHtml(CM.fmtTime(memory.event_time))}</span></div>
        ${sourceContent}
        <div class="memory-actions">${actions}</div>
      </div>
    </details>`;
  };

  CM.handleMemoryAction = async button => {
    const id = Number(button.dataset.memoryId || 0);
    const memory = CM.state.runtimeMemories.find(item => Number(item.id) === id);
    if (!memory || !id) return;
    const characterId = CM.state.characterId;
    const base = `/v1/characters/${encodeURIComponent(characterId)}/memories/${id}`;
    const action = button.dataset.memoryAction;
    if (action === "forget") {
      if (!window.confirm("让这个人物不再把这条内容作为长期记忆 Recall？原始经历不会删除。")) return;
      await CM.api(`${base}/active`, {method:"POST", body:JSON.stringify({active:false})});
    } else if (action === "restore") {
      await CM.api(`${base}/active`, {method:"POST", body:JSON.stringify({active:true})});
    } else if (action === "pin") {
      await CM.api(`${base}/pin`, {method:"POST", body:JSON.stringify({pinned:!memory.pinned})});
    } else if (action === "correct") {
      const corrected = window.prompt("纠正后的记忆：原记忆会保留为历史版本，不修改原始 Event。", memory.content);
      if (corrected == null || !corrected.trim() || corrected.trim() === memory.content.trim()) return;
      await CM.api(`${base}/correct`, {method:"POST", body:JSON.stringify({content:corrected.trim()})});
    } else {
      return;
    }
    await CM.showRuntime();
  };

  CM.showRuntime = async () => {
    if (CM.isGroupConversation()) return;
    const requested = CM.state.characterId;
    CM.openDrawer(`${CM.currentProfile().name} · Runtime`, "先看核心状态；Memory、Intent 与内部上下文按需展开");
    CM.dom.drawerBody.innerHTML = "<p>正在加载…</p>";
    try {
      const [data, memoryData] = await Promise.all([
        CM.api(`/v1/runtime/${encodeURIComponent(requested)}`),
        CM.api(`/v1/characters/${encodeURIComponent(requested)}/memories?include_inactive=true&limit=80`),
      ]);
      if (CM.isGroupConversation() || requested !== CM.state.characterId) return;
      const p = data.provider || {};
      const intents = Array.isArray(data.intents) ? data.intents : [];
      CM.state.runtimeMemories = memoryData.memories || [];
      const activeMemories = CM.state.runtimeMemories.filter(item => item.active).length;
      CM.dom.drawerBody.innerHTML = `
        <section class="trace-summary-card runtime-summary-card">
          <div><span>Runtime</span><strong>${CM.escapeHtml(data.runtime_loaded ? "Loaded" : "Idle")}</strong></div>
          <div><span>Chat Model</span><strong>${CM.escapeHtml(p.chat_model || "—")}</strong></div>
          <div><span>Memory</span><strong>${activeMemories} active / ${CM.state.runtimeMemories.length} shown</strong></div>
          <div><span>Intent</span><strong>${intents.length}</strong></div>
        </section>

        <details class="inspector-details">
          <summary>Memory Inspector · 最近 ${CM.state.runtimeMemories.length} 条</summary>
          <p class="memory-help">固定会提高 Recall 优先级；忘记只停用派生 Memory，不删除原始经历；纠正会创建新版本并保留旧版本。</p>
          <div class="memory-list">${CM.state.runtimeMemories.map(CM.memoryCardHtml).join("") || "<p>暂无 Memory。</p>"}</div>
        </details>

        <details class="inspector-details">
          <summary>Intent / Task · 最近 ${intents.length} 条</summary>
          <pre>${CM.escapeHtml(JSON.stringify(intents, null, 2))}</pre>
        </details>

        <details class="inspector-details">
          <summary>Mental State</summary>
          <pre>${CM.escapeHtml(CM.hiddenIfEmpty(data.mental_state))}</pre>
        </details>

        <details class="inspector-details">
          <summary>Persona 原文</summary>
          <pre>${CM.escapeHtml(CM.hiddenIfEmpty(data.persona))}</pre>
        </details>

        <details class="inspector-details">
          <summary>Provider / Runtime 初始化信息</summary>
          <div class="kv">
            <div>Chat Model</div><div>${CM.escapeHtml(p.chat_model || "—")}</div>
            <div>Embedding</div><div>${CM.escapeHtml(`${p.embedding_provider || "—"} / ${p.embedding_model || "—"}`)}</div>
            <div>Runtime Loaded</div><div>${CM.escapeHtml(data.runtime_loaded)}</div>
          </div>
          <h4>初始化耗时</h4>
          ${CM.timingHtml(data.runtime_init_timings)}
        </details>`;
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  };

  CM.bootstrap = async () => {
    const d = CM.dom;
    d.drawerClose.addEventListener("click", CM.closeDrawer);
    d.drawerBackdrop.addEventListener("click", CM.closeDrawer);
    d.drawerBody.addEventListener("click", event => {
      const memoryAction = event.target.closest("[data-memory-action]");
      if (memoryAction) CM.handleMemoryAction(memoryAction).catch(error => {
        CM.dom.drawerBody.insertAdjacentHTML("afterbegin", `<div class="error">${CM.escapeHtml(error.message)}</div>`);
      });
    });
    d.runtimeButton.addEventListener("click", CM.showRuntime);
    d.characterList.addEventListener("click", event => {
      if (event.target.closest("[data-character-overflow]")) { CM.showCharacterOverflow(); return; }
      const button = event.target.closest("[data-character]");
      if (button) CM.switchCharacter(button.dataset.character).catch(console.error);
    });
    d.drawerBody.addEventListener("click", event => {
      const overflowCharacter = event.target.closest("[data-overflow-character]");
      if (overflowCharacter) {
        CM.closeDrawer();
        CM.switchCharacter(overflowCharacter.dataset.overflowCharacter).catch(console.error);
      }
    });
    d.chat.addEventListener("click", event => {
      const older = event.target.closest("[data-load-older-direct]");
      if (older) { CM.loadOlderDirectHistory().catch(console.error); return; }
      const thought = event.target.closest("[data-thought]");
      if (thought) { CM.showThought(thought.dataset.thought); return; }
      const trace = event.target.closest("[data-trace]");
      if (trace) CM.showTrace(trace.dataset.trace);
    });
    d.input.addEventListener("input", () => { d.input.style.height = "auto"; d.input.style.height = `${Math.min(d.input.scrollHeight, 150)}px`; });
    d.input.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); d.composer.requestSubmit(); } });
    d.composer.addEventListener("submit", event => { event.preventDefault(); CM.submitCurrentText().catch(error => console.error("submit failed", error)); });
    const updateNow = () => { d.nowText.textContent = new Date().toLocaleString([], {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"}); };
    updateNow();
    setInterval(updateNow, 30000);

    try {
      await CM.loadCharacters();
      await CM.emit("ready");
      await CM.loadHistory();
      if (!CM.isGroupConversation()) CM.connectDirectStream();
      CM.updateComposerState();
    } catch (error) {
      d.chat.innerHTML = `<div class="error">页面初始化失败：${CM.escapeHtml(error.message)}</div>`;
      console.error(error);
    }
  };

  window.addEventListener("beforeunload", () => CM.closeDirectStream());
  window.addEventListener("DOMContentLoaded", () => CM.bootstrap(), {once:true});
})();
