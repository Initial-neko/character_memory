(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before groups.js");

  let groups = [];
  let archivedGroups = [];
  const pending = new Set();
  let groupStream = null;
  let groupStreamId = null;
  let historyState = {groupId:null, messages:[], hasMore:false, nextBeforeId:null, loadingOlder:false};
  const section = document.getElementById("sidebarGroups");
  if (!section) throw new Error("sidebar group slot is missing");
  section.classList.add("group-section");
  section.innerHTML = `<div class="group-section-head"><span>群聊</span><span class="group-section-actions"><button class="group-archive-list-button" type="button" title="查看已归档群聊">归档</button><button class="group-create-button" type="button" title="创建群聊">＋</button></span></div><nav class="group-list" aria-label="群聊"></nav>`;
  const list = section.querySelector(".group-list");
  const createButton = section.querySelector(".group-create-button");
  const archiveListButton = section.querySelector(".group-archive-list-button");

  const activeId = () => CM.state.conversation.groupId;
  const current = () => groups.find(item => item.id === activeId()) || null;
  const initial = group => (group?.name || "群").trim().slice(0, 1).toUpperCase();

  function resetHistory(groupId) {
    historyState = {groupId, messages:[], hasMore:false, nextBeforeId:null, loadingOlder:false};
  }

  function closeStream() {
    groupStream?.close?.();
    groupStream = null;
    groupStreamId = null;
  }

  function closeMenus(exceptId = null) {
    list?.querySelectorAll("[data-group-menu]").forEach(menu => {
      if (exceptId && menu.dataset.groupMenu === exceptId) return;
      menu.classList.add("hidden");
    });
  }

  function renderList() {
    if (!list) return;
    if (!groups.length) {
      list.innerHTML = '<div class="group-members group-empty-hint">还没有群聊</div>';
      return;
    }
    list.innerHTML = groups.map(group => {
      const names = (group.members || []).map(item => item.name || item.id).join("、");
      const active = CM.isGroupConversation() && group.id === activeId();
      const id = CM.escapeHtml(group.id);
      return `<div class="group-item-wrap" data-group-row="${id}"><button class="group-item ${active ? "active" : ""}" type="button" data-group="${id}"><span class="group-avatar">${CM.escapeHtml(initial(group))}</span><span class="group-copy"><span class="group-name">${CM.escapeHtml(group.name)}</span><span class="group-members">${CM.escapeHtml(names)}</span></span></button><button class="group-more-button" type="button" data-group-more="${id}" title="群聊操作" aria-label="群聊操作">···</button><div class="group-context-menu hidden" data-group-menu="${id}"><button type="button" data-group-archive="${id}">归档</button></div></div>`;
    }).join("");
  }

  async function loadGroups() {
    const data = await CM.api("/v1/groups");
    groups = data.groups || [];
    renderList();
  }

  async function loadArchivedGroups() {
    const data = await CM.api("/v1/groups?archived=true");
    archivedGroups = data.groups || [];
    return archivedGroups;
  }

  function renderArchivedDrawer() {
    if (!archivedGroups.length) {
      CM.dom.drawerBody.innerHTML = '<p class="muted">还没有归档的群聊。</p>';
      return;
    }
    CM.dom.drawerBody.innerHTML = `<div class="group-archive-list">${archivedGroups.map(group => {
      const names = (group.members || []).map(item => item.name || item.id).join("、");
      const archivedAt = group.archived_at ? CM.fmtDate(group.archived_at) : "";
      return `<div class="group-archive-card"><div class="group-archive-copy"><strong>${CM.escapeHtml(group.name)}</strong><span>${CM.escapeHtml(names)}</span>${archivedAt ? `<small>归档于 ${CM.escapeHtml(archivedAt)}</small>` : ""}</div><button type="button" data-group-restore="${CM.escapeHtml(group.id)}">恢复</button></div>`;
    }).join("")}</div>`;
  }

  async function showArchivedGroups() {
    CM.openDrawer("已归档群聊", "归档只隐藏任务，不删除消息、Memory、Trace 或媒体；可随时恢复");
    CM.dom.drawerBody.innerHTML = "<p>正在读取归档群聊…</p>";
    try {
      await loadArchivedGroups();
      renderArchivedDrawer();
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  }

  async function archiveGroup(groupId) {
    if (!groupId) return;
    closeMenus();
    await CM.api(`/v1/groups/${encodeURIComponent(groupId)}/archive`, {method:"POST"});
    groups = groups.filter(item => item.id !== groupId);
    pending.delete(groupId);
    renderList();
    if (CM.isGroupConversation() && activeId() === groupId) {
      await CM.switchCharacter(CM.state.characterId);
    }
  }

  async function restoreArchivedGroup(groupId) {
    if (!groupId) return;
    await CM.api(`/v1/groups/${encodeURIComponent(groupId)}/restore`, {method:"POST"});
    archivedGroups = archivedGroups.filter(item => item.id !== groupId);
    await loadGroups();
    renderArchivedDrawer();
  }

  function groupMessageAvatar(message) {
    if (message.role === "user") return "";
    return String(message.actor_name || message.actor_id || "AI").trim().slice(0,1).toUpperCase();
  }

  function turnButton(message) {
    if (message.role !== "user" || !message.turn_id) return "";
    const summary = message.turn_summary || null;
    const label = summary
      ? `本轮反应 · ${summary.replied || 0} 回复 / ${summary.silent || 0} 沉默`
      : "本轮反应 · 等待判断";
    return `<button class="group-turn-debug" type="button" data-group-turn="${CM.escapeHtml(message.turn_id)}">${CM.escapeHtml(label)}</button>`;
  }

  function addMessage(message) {
    const row = document.createElement("article");
    row.className = `message-row ${message.role}${message.role === "assistant" ? " group-assistant" : ""}`;
    row.dataset.messageId = message.id ?? "";
    const speaker = message.role === "assistant" ? `<div class="group-speaker-name">${CM.escapeHtml(message.actor_name || message.actor_id)}</div>` : "";
    const avatar = groupMessageAvatar(message);
    const sticker = message.sticker || (message.sticker_id ? {id:message.sticker_id,label:message.sticker_label || "表情包",url:`/v1/stickers/${encodeURIComponent(message.sticker_id)}/asset`} : null);
    const isVoiceMessage = message.action === "VOICE_MESSAGE";
    const hideStoredResourceText = isVoiceMessage || (message.action === "STICKER" && sticker) || (message.action === "IMAGE" && message.image);
    const text = hideStoredResourceText ? "" : String(message.content || "").trim();
    const textHtml = text ? `<div class="bubble">${CM.escapeHtml(text)}</div>` : "";
    const stickerHtml = sticker?.url ? `<div class="sticker-bubble"><img class="group-message-sticker" src="${CM.escapeHtml(sticker.url)}" alt="${CM.escapeHtml(sticker.label || "表情包")}" loading="lazy"><span class="sticker-fallback">表情</span></div>` : "";
    const imageHtml = message.image?.url ? `<div class="image-bubble"><img class="group-message-image" src="${CM.escapeHtml(message.image.url)}" alt="${CM.escapeHtml(message.image.label || "图片")}" loading="lazy"><div class="image-caption">${CM.escapeHtml(message.image.label || "图片")}</div></div>` : "";
    const voiceHtml = CM.voiceMessageHtml(message);
    row.innerHTML = `<div class="avatar">${CM.escapeHtml(avatar)}</div><div class="bubble-wrap">${speaker}${textHtml}${stickerHtml}${imageHtml}${voiceHtml}<div class="message-meta"><span>${CM.fmtTime(message.event_time)}</span>${turnButton(message)}</div></div>`;
    row.querySelectorAll(".sticker-bubble img").forEach(img => img.addEventListener("error", () => img.closest(".sticker-bubble")?.classList.add("broken"), {once:true}));
    CM.bindVoiceMessage(row);
    CM.dom.chat.appendChild(row);
  }

  function renderHistory(messages, {preserveScroll = false} = {}) {
    const beforeHeight = document.body.scrollHeight;
    const beforeY = window.scrollY;
    CM.dom.chat.innerHTML = "";
    const group = current();
    if (!messages.length) {
      CM.dom.chat.innerHTML = `<div class="empty">「${CM.escapeHtml(group?.name || "群聊")}」还没有消息。<br>说第一句话，看看谁会接话。</div>`;
      if (pending.has(activeId())) appendPending("群成员正在输入…");
      return;
    }
    if (historyState.hasMore) {
      const older = document.createElement("div");
      older.className = "date-separator history-load-more";
      older.innerHTML = `<button class="detail-button" type="button" data-group-load-older ${historyState.loadingOlder ? "disabled" : ""}>${historyState.loadingOlder ? "正在加载…" : "加载更早的群消息"}</button>`;
      CM.dom.chat.appendChild(older);
    }
    let lastDate = null;
    for (const message of messages) {
      const date = CM.fmtDate(message.event_time);
      if (date !== lastDate) {
        const sep = document.createElement("div");
        sep.className = "date-separator";
        sep.textContent = `── ${date} ──`;
        CM.dom.chat.appendChild(sep);
        lastDate = date;
      }
      addMessage(message);
    }
    if (pending.has(activeId())) appendPending("群成员正在输入…");
    if (preserveScroll) {
      requestAnimationFrame(() => {
        const delta = document.body.scrollHeight - beforeHeight;
        window.scrollTo({top: beforeY + delta, behavior:"auto"});
      });
    } else CM.scrollToBottom(false);
  }

  function appendPending(text) {
    CM.dom.chat.querySelector(".group-pending-note")?.remove();
    const note = document.createElement("div");
    note.className = "group-pending-note";
    note.textContent = text;
    CM.dom.chat.appendChild(note);
  }

  function memberName(characterId) {
    const group = current();
    return group?.members?.find(item => item.id === characterId)?.name || characterId;
  }

  function rawCharacterEventToMessage(raw) {
    const metadata = raw.metadata || {};
    const stickerId = metadata.sticker_id || null;
    const imageId = metadata.image_id || null;
    return {
      id:raw.id,
      conversation_id:raw.conversation_id,
      turn_id:raw.turn_id,
      role:"assistant",
      actor_type:"CHARACTER",
      actor_id:raw.actor_id,
      actor_name:memberName(raw.actor_id),
      content:raw.content || "",
      event_time:raw.event_time,
      action:metadata.action,
      sticker_id:stickerId,
      sticker:stickerId ? {id:stickerId,label:metadata.sticker_label || "表情包",url:`/v1/stickers/${encodeURIComponent(stickerId)}/asset`} : null,
      image_id:imageId,
      image:imageId ? {id:imageId,label:metadata.image_label || "图片",url:`/v1/images/${encodeURIComponent(raw.actor_id)}/${encodeURIComponent(imageId)}/asset`} : null,
      voice_status:metadata.voice_status || null,
      voice_media_id:metadata.voice_media_id || null,
      voice_duration_ms:metadata.voice_duration_ms ?? null,
      voice_error:metadata.voice_error || null,
      source_conversation_event_id:metadata.source_conversation_event_id,
    };
  }

  function mergeMessage(message) {
    if (!message) return;
    const index = message.id == null ? -1 : historyState.messages.findIndex(item => item.id === message.id);
    if (index >= 0) historyState.messages[index] = {...historyState.messages[index], ...message};
    else historyState.messages.push(message);
    historyState.messages.sort((a, b) => Number(a.id || 0) - Number(b.id || 0));
  }

  function updateTurnSummary(turnId, silent) {
    const source = historyState.messages.find(item => item.role === "user" && item.turn_id === turnId);
    if (!source) return;
    const currentSummary = source.turn_summary || {total:0,replied:0,silent:0};
    source.turn_summary = {
      total:(currentSummary.total || 0) + 1,
      replied:(currentSummary.replied || 0) + (silent ? 0 : 1),
      silent:(currentSummary.silent || 0) + (silent ? 1 : 0),
    };
  }

  async function reconcileLatest(groupId) {
    const requested = groupId || activeId();
    if (!requested || !CM.isGroupConversation() || requested !== activeId()) return;
    const data = await CM.api(`/v1/groups/${encodeURIComponent(requested)}/history?limit=50`);
    if (!CM.isGroupConversation() || requested !== activeId()) return;
    const index = groups.findIndex(item => item.id === requested);
    if (index >= 0 && data.group) groups[index] = data.group;
    if (historyState.groupId !== requested) resetHistory(requested);
    for (const message of data.messages || []) mergeMessage(message);
    renderList();
    renderHistory(historyState.messages, {preserveScroll:true});
  }

  function connectStream(groupId) {
    if (groupStream && groupStreamId === groupId) return;
    closeStream();
    const params = new URLSearchParams({scope:"group", conversation_id:groupId});
    const source = new EventSource(`/v1/events/stream?${params.toString()}`);
    groupStream = source;
    groupStreamId = groupId;
    source.addEventListener("open", () => {
      reconcileLatest(groupId).catch(error => console.warn("[sse reconcile group]", error));
    });
    source.addEventListener("reaction_status", event => {
      if (!CM.isGroupConversation() || groupId !== activeId()) return;
      const data = JSON.parse(event.data || "{}");
      if (["queued","typing","superseded"].includes(data.state)) pending.add(groupId);
      if (data.state === "idle") pending.delete(groupId);
      CM.updateHeader();
      renderHistory(historyState.messages);
    });
    source.addEventListener("group_character_event", event => {
      if (!CM.isGroupConversation() || groupId !== activeId()) return;
      mergeMessage(rawCharacterEventToMessage(JSON.parse(event.data)));
      renderHistory(historyState.messages);
    });
    source.addEventListener("group_member_complete", event => {
      if (!CM.isGroupConversation() || groupId !== activeId()) return;
      const data = JSON.parse(event.data || "{}");
      updateTurnSummary(data.turn_id, Boolean(data.silent));
      renderHistory(historyState.messages);
    });
    source.addEventListener("reaction_error", event => {
      if (!CM.isGroupConversation() || groupId !== activeId()) return;
      const data = JSON.parse(event.data || "{}");
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `群聊生成失败：${data.message || "未知错误"}`;
      CM.dom.chat.appendChild(box);
    });
  }

  async function loadHistory({beforeId = null, appendOlder = false} = {}) {
    const requested = activeId();
    if (!requested) return;
    const params = new URLSearchParams({limit:"50"});
    if (beforeId != null) params.set("before_id", String(beforeId));
    const data = await CM.api(`/v1/groups/${encodeURIComponent(requested)}/history?${params.toString()}`);
    if (!CM.isGroupConversation() || requested !== activeId()) return;
    const index = groups.findIndex(item => item.id === requested);
    if (index >= 0 && data.group) groups[index] = data.group;
    if (historyState.groupId !== requested) resetHistory(requested);
    const incoming = data.messages || [];
    if (appendOlder) {
      const existing = new Set(historyState.messages.map(item => item.id));
      historyState.messages = [...incoming.filter(item => !existing.has(item.id)), ...historyState.messages];
    } else historyState.messages = incoming;
    historyState.hasMore = Boolean(data.has_more);
    historyState.nextBeforeId = data.next_before_id ?? null;
    renderList();
    renderHistory(historyState.messages, {preserveScroll:appendOlder});
  }

  async function loadOlderHistory() {
    if (!CM.isGroupConversation() || historyState.loadingOlder || !historyState.hasMore || historyState.nextBeforeId == null) return;
    historyState.loadingOlder = true;
    renderHistory(historyState.messages, {preserveScroll:true});
    try { await loadHistory({beforeId:historyState.nextBeforeId, appendOlder:true}); }
    finally { historyState.loadingOlder = false; renderHistory(historyState.messages, {preserveScroll:true}); }
  }

  function applyHeader() {
    if (!CM.isGroupConversation()) return false;
    const group = current();
    if (!group) return false;
    const names = (group.members || []).map(item => item.name || item.id).join("、");
    const isPending = pending.has(group.id);
    CM.dom.characterName.textContent = group.name;
    CM.dom.characterIdentity.textContent = `${names}${isPending ? " · 有人正在输入" : ""}`;
    CM.dom.headerAvatar.textContent = initial(group);
    CM.dom.input.placeholder = `发到「${group.name}」`;
    CM.dom.runtimeButton.disabled = true;
    CM.dom.runtimeButton.title = "群聊心理活动请查看每条用户消息下方的「本轮反应」";
    CM.features.intent?.setDisabled?.(true);
    return true;
  }

  function applyComposerState() {
    if (!CM.isGroupConversation()) return false;
    CM.dom.sendButton.disabled = false;
    CM.dom.input.disabled = false;
    CM.features.stickers?.setDisabled?.(false);
    CM.features.images?.setDisabled?.(false);
    CM.dom.input.focus();
    return true;
  }

  async function enter(groupId) {
    const group = groups.find(item => item.id === groupId);
    if (!group) return;
    CM.closeDirectStream?.();
    closeStream();
    CM.state.conversation = {type:"GROUP", groupId};
    CM.state.lastRenderedSignature = "";
    resetHistory(groupId);
    document.body.classList.add("group-mode");
    CM.closeDrawer();
    CM.features.stickers?.close?.();
    CM.features.images?.close?.();
    CM.renderCharacterList();
    renderList();
    CM.updateHeader();
    CM.dom.chat.innerHTML = '<div class="empty">正在加载群聊记录…</div>';
    await loadHistory();
    connectStream(groupId);
    CM.updateComposerState();
    await CM.emit("conversationChanged", {type:"GROUP", groupId});
  }

  function leave() {
    if (!CM.isGroupConversation()) return;
    closeStream();
    pending.delete(activeId());
    CM.state.conversation = {type:"DIRECT", groupId:null};
    document.body.classList.remove("group-mode");
    renderList();
  }

  async function commitSend(groupId, payload) {
    if (!groupId) return;
    connectStream(groupId);
    try {
      const result = await CM.api(`/v1/groups/${encodeURIComponent(groupId)}/messages`, {method:"POST", body:JSON.stringify(payload)});
      if (CM.isGroupConversation() && groupId === activeId()) {
        mergeMessage(result.message);
        renderHistory(historyState.messages);
      }
      return result;
    } catch (error) {
      console.error("[group message failed]", groupId, error);
      if (CM.isGroupConversation() && groupId === activeId()) {
        const box = document.createElement("div");
        box.className = "error";
        box.textContent = `发送失败：${error.message}`;
        CM.dom.chat.appendChild(box);
      }
      throw error;
    }
  }

  async function sendText(message) {
    const groupId = activeId();
    if (!groupId || !message.trim()) return;
    CM.dom.input.value = "";
    CM.dom.input.style.height = "auto";
    return commitSend(groupId, {message});
  }

  async function sendSticker(sticker) {
    const groupId = activeId();
    if (!groupId || !sticker) return;
    CM.features.stickers?.close?.();
    return commitSend(groupId, {message:"",sticker_id:sticker.id});
  }

  async function sendImage(draft, caption) {
    const groupId = activeId();
    if (!groupId || !draft) return;
    return commitSend(groupId, {message:caption,image:{filename:draft.filename,data_url:draft.data_url}});
  }

  function showCreateGroup() {
    CM.openDrawer("创建群聊", "选择 2～4 个已经存在的人物");
    CM.dom.drawerBody.innerHTML = `<div class="group-create-form"><label>群名称<input type="text" data-group-name maxlength="80" value="新群聊"></label><div><strong>选择成员</strong><div class="group-member-picker">${CM.state.characters.map(profile => `<label class="group-member-option"><input type="checkbox" data-group-member value="${CM.escapeHtml(profile.id)}"><span>${CM.escapeHtml(profile.name)} · ${CM.escapeHtml(profile.identity || profile.tagline || "")}</span></label>`).join("")}</div></div><div class="error hidden" data-group-create-error></div><div class="group-create-actions"><button type="button" data-group-create-cancel>取消</button><button type="button" class="primary" data-group-create-confirm>创建群聊</button></div></div>`;
  }

  async function createGroupFromDrawer() {
    const name = CM.dom.drawerBody.querySelector("[data-group-name]")?.value.trim() || "新群聊";
    const memberIds = [...CM.dom.drawerBody.querySelectorAll("[data-group-member]:checked")].map(el => el.value);
    const errorBox = CM.dom.drawerBody.querySelector("[data-group-create-error]");
    if (memberIds.length < 2 || memberIds.length > 4) {
      if (errorBox) { errorBox.textContent = "请选择 2～4 个群成员。"; errorBox.classList.remove("hidden"); }
      return;
    }
    try {
      const data = await CM.api("/v1/groups", {method:"POST", body:JSON.stringify({name,member_ids:memberIds})});
      groups.unshift(data.group);
      CM.closeDrawer();
      renderList();
      await enter(data.group.id);
    } catch (error) {
      if (errorBox) { errorBox.textContent = error.message; errorBox.classList.remove("hidden"); }
    }
  }

  async function showTurn(turnId) {
    const group = current();
    if (!group) return;
    CM.openDrawer(`${group.name} · 本轮反应`, "每个人独立决定发言或保持沉默；这里只展示安全心理摘要，不展示隐藏思维链");
    CM.dom.drawerBody.innerHTML = "<p>正在读取群成员反应…</p>";
    try {
      const data = await CM.api(`/v1/groups/${encodeURIComponent(group.id)}/turns/${encodeURIComponent(turnId)}/traces`);
      const traces = data.traces || [];
      CM.dom.drawerBody.innerHTML = traces.length ? `<div class="card-list">${traces.map(item => {
        const actions = item.actions || [];
        const actionText = actions.length ? actions.map(action => action.type).join(" / ") : "SILENCE";
        const stickerCount = item.sticker_retrieval?.matches?.length || 0;
        return `<div class="card"><strong>${CM.escapeHtml(item.character_name)}</strong><span> · ${CM.escapeHtml(actionText)} · ${CM.escapeHtml(CM.fmtMs(item.total_ms))}</span><div><b>注意：</b>${CM.escapeHtml(CM.hiddenIfEmpty(item.perception))}</div><div><b>反应：</b>${CM.escapeHtml(CM.hiddenIfEmpty(item.reaction))}</div><div class="muted">Sticker 候选：${CM.escapeHtml(stickerCount)}</div>${item.created_memory_ids?.length ? `<div>Memory: ${CM.escapeHtml(item.created_memory_ids.join(", "))}</div>` : ""}</div>`;
      }).join("")}</div>` : '<p class="muted">这一条消息可能被合并进后续连续表达，或者成员还没有完成判断。</p>';
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  }

  createButton?.addEventListener("click", showCreateGroup);
  archiveListButton?.addEventListener("click", () => showArchivedGroups().catch(console.error));
  list?.addEventListener("click", event => {
    const archive = event.target.closest("[data-group-archive]");
    if (archive) {
      event.preventDefault();
      event.stopPropagation();
      archiveGroup(archive.dataset.groupArchive).catch(error => console.error("archive group failed", error));
      return;
    }
    const more = event.target.closest("[data-group-more]");
    if (more) {
      event.preventDefault();
      event.stopPropagation();
      const groupId = more.dataset.groupMore;
      const menu = list.querySelector(`[data-group-menu="${CSS.escape(groupId)}"]`);
      const wasHidden = menu?.classList.contains("hidden") ?? true;
      closeMenus();
      if (wasHidden) menu?.classList.remove("hidden");
      return;
    }
    const button = event.target.closest("[data-group]");
    if (button) {
      closeMenus();
      enter(button.dataset.group).catch(console.error);
    }
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".group-item-wrap")) closeMenus();
  });
  CM.dom.drawerBody.addEventListener("click", event => {
    if (event.target.closest("[data-group-create-cancel]")) CM.closeDrawer();
    if (event.target.closest("[data-group-create-confirm]")) createGroupFromDrawer().catch(console.error);
    const restore = event.target.closest("[data-group-restore]");
    if (restore) restoreArchivedGroup(restore.dataset.groupRestore).catch(error => console.error("restore group failed", error));
  });
  CM.dom.chat.addEventListener("click", event => {
    if (!CM.isGroupConversation()) return;
    const older = event.target.closest("[data-group-load-older]");
    if (older) { loadOlderHistory().catch(console.error); return; }
    const button = event.target.closest("[data-group-turn]");
    if (button) showTurn(button.dataset.groupTurn).catch(console.error);
  });

  CM.registerFeature("groups", {loadGroups,loadArchivedGroups,loadHistory,loadOlderHistory,reconcileLatest,enter,leave,current,applyHeader,applyComposerState,sendText,sendSticker,sendImage,archiveGroup,restoreArchivedGroup,showArchivedGroups,renderList,closeStream});
  CM.on("ready", loadGroups);
  window.addEventListener("beforeunload", closeStream);
})();
