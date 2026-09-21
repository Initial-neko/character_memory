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

  CM.voiceMessageHtml = message => {
    if (message.action !== "VOICE_MESSAGE") return "";
    const status = message.voice_status || "pending";
    const durationMs = Number(message.voice_duration_ms || 0);
    const seconds = durationMs > 0 ? Math.max(1, Math.round(durationMs / 1000)) : 0;
    const width = Math.min(260, 92 + Math.min(seconds || 1, 34) * 5);
    const mediaUrl = message.voice_media_id ? `/v1/media/${encodeURIComponent(message.voice_media_id)}` : "";
    if (status === "failed") {
      return `<div class="voice-message voice-failed"><div class="voice-bubble voice-disabled">⚠ 语音生成失败</div><div class="voice-tools"><button type="button" data-voice-text>文本</button></div><div class="voice-transcript hidden" data-voice-transcript>${CM.escapeHtml(message.content || "")}</div></div>`;
    }
    if (status !== "ready" || !mediaUrl) {
      return `<div class="voice-message voice-pending"><div class="voice-bubble voice-disabled"><span class="voice-glyph">)))</span><span>语音生成中…</span></div></div>`;
    }
    return `<div class="voice-message" data-voice-message="${CM.escapeHtml(message.id)}">
      <button class="voice-bubble" type="button" data-voice-play data-audio-url="${CM.escapeHtml(mediaUrl)}" style="--voice-width:${width}px" aria-label="播放语音消息">
        <span class="voice-glyph" aria-hidden="true">)))</span><span class="voice-duration">${seconds || "?"}"</span>
      </button>
      <div class="voice-tools"><button type="button" data-voice-text>文本</button><button type="button" data-voice-translation>翻译</button></div>
      <div class="voice-transcript hidden" data-voice-transcript>${CM.escapeHtml(message.content || "")}</div>
      <div class="voice-translation hidden" data-voice-translation-panel>暂无翻译</div>
    </div>`;
  };

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
    row.querySelector("[data-voice-text]")?.addEventListener("click", () => row.querySelector("[data-voice-transcript]")?.classList.toggle("hidden"));
    row.querySelector("[data-voice-translation]")?.addEventListener("click", () => row.querySelector("[data-voice-translation-panel]")?.classList.toggle("hidden"));
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
      ? `<button class="detail-button" type="button" data-trace="${message.source_event_id}" title="查看本轮开发详情">···</button>` : "";
    const sticker = message.sticker || (message.sticker_id ? {id:message.sticker_id,label:message.sticker_label || "表情包",url:`/v1/stickers/${encodeURIComponent(message.sticker_id)}/asset`} : null);
    const image = message.image || null;
    const isVoiceMessage = message.action === "VOICE_MESSAGE";
    const hideStoredResourceText = isVoiceMessage || (message.action === "STICKER" && sticker) || (message.action === "IMAGE" && image);
    const text = hideStoredResourceText ? "" : String(message.content || "").trim();
    const textHtml = text ? `<div class="bubble">${CM.escapeHtml(text)}</div>` : "";
    const stickerHtml = sticker?.url ? `<div class="sticker-bubble"><img src="${CM.escapeHtml(sticker.url)}" alt="${CM.escapeHtml(sticker.label || "表情包")}" loading="lazy"><span class="sticker-fallback">表情</span></div>` : "";
    const imageHtml = image?.url ? `<div class="image-bubble"><img src="${CM.escapeHtml(image.url)}" alt="${CM.escapeHtml(image.label || "图片")}" loading="lazy"><div class="image-caption">${CM.escapeHtml(image.label || "图片")}</div></div>` : "";
    const voiceHtml = CM.voiceMessageHtml(message);
    const proactive = message.proactive || message.action === "PROACTIVE_MESSAGE";
    row.innerHTML = `<div class="avatar">${CM.escapeHtml(avatar)}</div><div class="bubble-wrap">${textHtml}${stickerHtml}${imageHtml}${voiceHtml}<div class="message-meta"><span>${CM.fmtTime(message.event_time)}</span>${proactive ? '<span class="proactive-badge">主动消息</span>' : ""}${thought}${trace}</div></div>`;
    row.querySelectorAll(".sticker-bubble img").forEach(img => img.addEventListener("error", () => img.closest(".sticker-bubble")?.classList.add("broken"), {once:true}));
    CM.bindVoiceMessage(row);
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
    CM.dom.characterList.innerHTML = CM.state.characters.map(profile => {
      const pending = CM.state.pendingCharacters.has(profile.id);
      const hasUnread = unread?.isUnread?.(profile.id) || false;
      const preview = unread?.preview?.(profile) || profile.tagline || profile.identity || "Persistent AI Person";
      const active = !CM.isGroupConversation() && profile.id === CM.state.characterId;
      const id = CM.escapeHtml(profile.id);
      return `<div class="character-item-wrap" data-character-row="${id}"><button class="character-item ${active ? "active" : ""}" type="button" data-character="${id}"><span class="character-avatar">${CM.escapeHtml(CM.initialFor(profile))}</span><span class="character-copy"><span class="character-name character-name-line"><span>${CM.escapeHtml(profile.name)}${pending ? '<span class="character-pending"> · 输入中</span>' : ""}</span>${hasUnread ? '<span class="unread-dot" title="有新消息" aria-label="有新消息"></span>' : ""}</span><span class="character-tagline character-preview">${CM.escapeHtml(preview)}</span></span></button><button class="character-more-button" type="button" data-character-more="${id}" title="角色操作" aria-label="角色操作">···</button><div class="character-context-menu hidden" data-character-menu="${id}"><button type="button" data-character-archive="${id}">归档人物…</button></div></div>`;
    }).join("");
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
    try { await CM.sendDirectPayload({message}); }
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

  CM.showTrace = async sourceEventId => {
    CM.openDrawer(`${CM.currentProfile().name} · 本轮详情`, `source_event_id = ${sourceEventId}`);
    CM.dom.drawerBody.innerHTML = "<p>正在加载…</p>";
    try {
      const trace = await CM.api(`/v1/traces/${sourceEventId}`);
      const actions = Array.isArray(trace.actions) ? trace.actions : (trace.action ? [trace.action] : []);
      const actionText = actions.length ? actions.map((action, index) => `${index + 1}. ${action.type}: ${action.message || action.sticker_id || action.image_id || action.reason || ""}`).join("\n") : "没有发送消息";
      const firstAction = trace.action || actions[0] || {};
      CM.dom.drawerBody.innerHTML = `<section class="section"><h3>耗时</h3>${CM.timingHtml(trace.timings)}</section><section class="section"><h3>决策</h3><div class="kv"><div>Actions</div><div>${CM.escapeHtml(actions.map(a => a.type).join(" / ") || "NO_REPLY")}</div><div>Action Reason</div><div>${CM.escapeHtml(CM.hiddenIfEmpty(firstAction.reason))}</div><div>Perception</div><div>${CM.escapeHtml(CM.hiddenIfEmpty(trace.perception))}</div><div>Reaction</div><div>${CM.escapeHtml(CM.hiddenIfEmpty(trace.reaction))}</div><div>Model Attempt</div><div>${CM.escapeHtml(trace.model_attempt || "—")}</div></div></section><section class="section"><h3>最终对外表达</h3><pre>${CM.escapeHtml(actionText)}</pre></section><section class="section"><h3>Sticker Retrieval</h3><pre>${CM.escapeHtml(JSON.stringify(trace.sticker_retrieval || {}, null, 2))}</pre></section><section class="section"><h3>Mental State · Before</h3><pre>${CM.escapeHtml(CM.hiddenIfEmpty(trace.mental_state_before))}</pre></section><section class="section"><h3>Mental State · After</h3><pre>${CM.escapeHtml(CM.hiddenIfEmpty(trace.mental_state_after))}</pre></section><section class="section"><h3>Recall</h3><div class="card-list">${(trace.recalled_memories || []).map(m => `<div class="card"><strong>${CM.escapeHtml(m.memory_type)}</strong><span> · importance ${CM.escapeHtml(m.importance)}</span><div>${CM.escapeHtml(m.content)}</div></div>`).join("") || "<p>本轮没有 Recall 到 Memory。</p>"}</div></section><section class="section"><h3>实际发送给模型的 messages</h3>${(trace.model_messages || []).map((m, i) => `<p><strong>${i + 1}. ${CM.escapeHtml(m.role)}</strong></p><pre>${CM.escapeHtml(m.content)}</pre>`).join("") || `<p>${CM.escapeHtml(hiddenText)}</p>`}</section><section class="section"><h3>Compiled Context</h3><pre>${CM.escapeHtml(CM.hiddenIfEmpty(trace.context))}</pre></section><section class="section"><h3>Memory Admission</h3><pre>${CM.escapeHtml(JSON.stringify(trace.memory_decisions || [], null, 2))}</pre></section><section class="section"><h3>Memory Write</h3><pre>${CM.escapeHtml(JSON.stringify({candidates:trace.memory_candidates || [], created_memory_ids:trace.created_memory_ids || []}, null, 2))}</pre></section><section class="section"><h3>Intent</h3><pre>${CM.escapeHtml(JSON.stringify({candidates:trace.intent_candidates || [], created_intent_ids:trace.created_intent_ids || []}, null, 2))}</pre></section><section class="section"><h3>Raw Model Response</h3><pre>${CM.escapeHtml(CM.hiddenIfEmpty(trace.raw_model_response))}</pre></section>`;
    } catch (error) { CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`; }
  };

  CM.showRuntime = async () => {
    if (CM.isGroupConversation()) return;
    const requested = CM.state.characterId;
    CM.openDrawer(`${CM.currentProfile().name} · Runtime`, "按需读取当前人物状态");
    CM.dom.drawerBody.innerHTML = "<p>正在加载…</p>";
    try {
      const data = await CM.api(`/v1/runtime/${encodeURIComponent(requested)}`);
      if (CM.isGroupConversation() || requested !== CM.state.characterId) return;
      const p = data.provider || {};
      CM.dom.drawerBody.innerHTML = `<section class="section"><h3>Runtime 初始化耗时</h3>${CM.timingHtml(data.runtime_init_timings)}</section><section class="section"><h3>Provider</h3><div class="kv"><div>Chat Model</div><div>${CM.escapeHtml(p.chat_model)}</div><div>Embedding</div><div>${CM.escapeHtml(`${p.embedding_provider} / ${p.embedding_model}`)}</div><div>Runtime Loaded</div><div>${CM.escapeHtml(data.runtime_loaded)}</div></div></section><section class="section"><h3>Mental State</h3><pre>${CM.escapeHtml(CM.hiddenIfEmpty(data.mental_state))}</pre></section><section class="section"><h3>Persona</h3><pre>${CM.escapeHtml(CM.hiddenIfEmpty(data.persona))}</pre></section><section class="section"><h3>Memory · 最近 ${data.memories.length} 条</h3><div class="card-list">${data.memories.map(m => `<div class="card"><strong>${CM.escapeHtml(m.memory_type)}</strong><span> · ${CM.escapeHtml(CM.fmtTime(m.event_time))}</span><div>${CM.escapeHtml(m.content)}</div></div>`).join("") || "<p>暂无 Memory。</p>"}</div></section><section class="section"><h3>Intent · 最近 ${data.intents.length} 条</h3><pre>${CM.escapeHtml(JSON.stringify(data.intents, null, 2))}</pre></section>`;
    } catch (error) { CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`; }
  };

  CM.bootstrap = async () => {
    const d = CM.dom;
    d.drawerClose.addEventListener("click", CM.closeDrawer);
    d.drawerBackdrop.addEventListener("click", CM.closeDrawer);
    d.runtimeButton.addEventListener("click", CM.showRuntime);
    d.characterList.addEventListener("click", event => { const button = event.target.closest("[data-character]"); if (button) CM.switchCharacter(button.dataset.character).catch(console.error); });
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
