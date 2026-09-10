(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before groups.js");

  let groups = [];
  const pending = new Set();
  const sidebar = document.querySelector(".sidebar");
  const sidebarFoot = document.querySelector(".sidebar-foot");
  const section = document.createElement("section");
  section.className = "group-section";
  section.innerHTML = `<div class="group-section-head"><span>Groups</span><button class="group-create-button" type="button" title="创建群聊">＋</button></div><nav class="group-list" aria-label="Groups"></nav>`;
  sidebar?.insertBefore(section, sidebarFoot || null);
  const list = section.querySelector(".group-list");
  const createButton = section.querySelector(".group-create-button");

  const activeId = () => CM.state.conversation.groupId;
  const current = () => groups.find(item => item.id === activeId()) || null;
  const initial = group => (group?.name || "群").trim().slice(0, 1).toUpperCase();

  function renderList() {
    if (!list) return;
    if (!groups.length) {
      list.innerHTML = '<div class="group-members" style="padding:6px 12px">还没有群聊</div>';
      return;
    }
    list.innerHTML = groups.map(group => {
      const names = (group.members || []).map(item => item.name || item.id).join("、");
      const active = CM.isGroupConversation() && group.id === activeId();
      return `<button class="group-item ${active ? "active" : ""}" type="button" data-group="${CM.escapeHtml(group.id)}"><span class="group-avatar">${CM.escapeHtml(initial(group))}</span><span class="group-copy"><span class="group-name">${CM.escapeHtml(group.name)}</span><span class="group-members">${CM.escapeHtml(names)}</span></span></button>`;
    }).join("");
  }

  async function loadGroups() {
    const data = await CM.api("/v1/groups");
    groups = data.groups || [];
    renderList();
  }

  function groupMessageAvatar(message) {
    if (message.role === "user") return "";
    return String(message.actor_name || message.actor_id || "AI").trim().slice(0,1).toUpperCase();
  }

  function addMessage(message) {
    const row = document.createElement("article");
    row.className = `message-row ${message.role}${message.role === "assistant" ? " group-assistant" : ""}`;
    row.dataset.messageId = message.id ?? "";
    const speaker = message.role === "assistant" ? `<div class="group-speaker-name">${CM.escapeHtml(message.actor_name || message.actor_id)}</div>` : "";
    const avatar = groupMessageAvatar(message);
    const sticker = message.sticker || (message.sticker_id ? {id:message.sticker_id,label:message.sticker_label || "表情包",url:`/v1/stickers/${encodeURIComponent(message.sticker_id)}/asset`} : null);
    const hideStoredResourceText = (message.action === "STICKER" && sticker) || (message.action === "IMAGE" && message.image);
    const text = hideStoredResourceText ? "" : String(message.content || "").trim();
    const textHtml = text ? `<div class="bubble">${CM.escapeHtml(text)}</div>` : "";
    const stickerHtml = sticker?.url ? `<div class="sticker-bubble"><img class="group-message-sticker" src="${CM.escapeHtml(sticker.url)}" alt="${CM.escapeHtml(sticker.label || "表情包")}" loading="lazy"><span class="sticker-fallback">表情</span></div>` : "";
    const imageHtml = message.image?.url ? `<div class="image-bubble"><img class="group-message-image" src="${CM.escapeHtml(message.image.url)}" alt="${CM.escapeHtml(message.image.label || "图片")}" loading="lazy"><div class="image-caption">${CM.escapeHtml(message.image.label || "图片")}</div></div>` : "";
    const turnDebug = message.role === "user" && message.turn_id ? `<button class="group-turn-debug" type="button" data-group-turn="${CM.escapeHtml(message.turn_id)}">本轮反应</button>` : "";
    row.innerHTML = `<div class="avatar">${CM.escapeHtml(avatar)}</div><div class="bubble-wrap">${speaker}${textHtml}${stickerHtml}${imageHtml}<div class="message-meta"><span>${CM.fmtTime(message.event_time)}</span>${turnDebug}</div></div>`;
    row.querySelectorAll(".sticker-bubble img").forEach(img => img.addEventListener("error", () => img.closest(".sticker-bubble")?.classList.add("broken"), {once:true}));
    CM.dom.chat.appendChild(row);
  }

  function renderHistory(messages) {
    CM.dom.chat.innerHTML = "";
    const group = current();
    if (!messages.length) {
      CM.dom.chat.innerHTML = `<div class="empty">「${CM.escapeHtml(group?.name || "群聊")}」还没有消息。<br>说第一句话，看看谁会接话。</div>`;
      return;
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
    if (pending.has(activeId())) appendPending("大家正在看这条消息…");
    CM.scrollToBottom(false);
  }

  function appendPending(text) {
    CM.dom.chat.querySelector(".group-pending-note")?.remove();
    const note = document.createElement("div");
    note.className = "group-pending-note";
    note.textContent = text;
    CM.dom.chat.appendChild(note);
  }

  async function loadHistory() {
    const requested = activeId();
    if (!requested) return;
    const data = await CM.api(`/v1/groups/${encodeURIComponent(requested)}/history?limit=180`);
    if (!CM.isGroupConversation() || requested !== activeId()) return;
    const index = groups.findIndex(item => item.id === requested);
    if (index >= 0 && data.group) groups[index] = data.group;
    renderList();
    renderHistory(data.messages || []);
  }

  function applyHeader() {
    if (!CM.isGroupConversation()) return false;
    const group = current();
    if (!group) return false;
    const names = (group.members || []).map(item => item.name || item.id).join("、");
    const isPending = pending.has(group.id);
    CM.dom.characterName.textContent = group.name;
    CM.dom.characterIdentity.textContent = `${names}${isPending ? " · 大家正在回复" : ""}`;
    CM.dom.headerAvatar.textContent = initial(group);
    CM.dom.input.placeholder = isPending ? "群成员正在回复…" : `发到「${group.name}」`;
    CM.dom.runtimeButton.disabled = true;
    CM.dom.runtimeButton.title = "群聊请使用每轮反应 Inspector";
    CM.features.intent?.setDisabled?.(true);
    return true;
  }

  function applyComposerState() {
    if (!CM.isGroupConversation()) return false;
    const isPending = pending.has(activeId());
    CM.dom.sendButton.disabled = isPending;
    CM.dom.input.disabled = isPending;
    CM.features.stickers?.setDisabled?.(isPending);
    CM.features.images?.setDisabled?.(isPending);
    if (!isPending) CM.dom.input.focus();
    return true;
  }

  async function enter(groupId) {
    const group = groups.find(item => item.id === groupId);
    if (!group) return;
    CM.state.conversation = {type:"GROUP", groupId};
    CM.state.lastRenderedSignature = "";
    document.body.classList.add("group-mode");
    CM.closeDrawer();
    CM.features.stickers?.close?.();
    CM.features.images?.close?.();
    CM.renderCharacterList();
    renderList();
    CM.updateHeader();
    CM.dom.chat.innerHTML = '<div class="empty">正在加载群聊记录…</div>';
    await loadHistory();
    CM.updateComposerState();
    await CM.emit("conversationChanged", {type:"GROUP", groupId});
  }

  function leave() {
    if (!CM.isGroupConversation()) return;
    CM.state.conversation = {type:"DIRECT", groupId:null};
    document.body.classList.remove("group-mode");
    renderList();
  }

  async function commitSend(groupId, payload, optimisticMessage, pendingText) {
    if (!groupId || pending.has(groupId)) return;
    pending.add(groupId);
    CM.updateHeader();
    if (CM.dom.chat.querySelector(".empty")) CM.dom.chat.innerHTML = "";
    if (optimisticMessage) addMessage(optimisticMessage);
    appendPending(pendingText);
    CM.scrollToBottom();
    try {
      const result = await CM.api(`/v1/groups/${encodeURIComponent(groupId)}/chat`, {method:"POST", body:JSON.stringify(payload)});
      if (CM.isGroupConversation() && groupId === activeId()) {
        const index = groups.findIndex(item => item.id === groupId);
        if (index >= 0 && result.group) groups[index] = result.group;
        renderHistory(result.messages || []);
        renderList();
      }
      return result;
    } catch (error) {
      console.error("[group chat failed]", groupId, error);
      if (CM.isGroupConversation() && groupId === activeId()) {
        CM.dom.chat.querySelector(".group-pending-note")?.remove();
        const box = document.createElement("div");
        box.className = "error";
        box.textContent = `群聊生成失败：${error.message}`;
        CM.dom.chat.appendChild(box);
      }
      throw error;
    } finally {
      pending.delete(groupId);
      CM.updateHeader();
      if (CM.isGroupConversation() && groupId === activeId()) await loadHistory().catch(console.warn);
    }
  }

  async function sendText(message) {
    const groupId = activeId();
    if (!groupId || !message.trim()) return;
    CM.dom.input.value = "";
    CM.dom.input.style.height = "auto";
    return commitSend(groupId, {message}, {role:"user",actor_type:"USER",actor_id:"user",actor_name:"我",content:message,event_time:new Date().toISOString()}, "大家正在看这条消息…");
  }

  async function sendSticker(sticker) {
    const groupId = activeId();
    if (!groupId || !sticker) return;
    CM.features.stickers?.close?.();
    return commitSend(groupId, {message:"",sticker_id:sticker.id}, {role:"user",actor_type:"USER",actor_id:"user",actor_name:"我",content:"",action:"STICKER",sticker_id:sticker.id,sticker,event_time:new Date().toISOString()}, "大家正在看这个表情…");
  }

  async function sendImage(draft, caption) {
    const groupId = activeId();
    if (!groupId || !draft) return;
    return commitSend(groupId, {message:caption,image:{filename:draft.filename,data_url:draft.data_url}}, {role:"user",actor_type:"USER",actor_id:"user",actor_name:"我",content:caption,image:{url:draft.data_url,label:draft.filename},event_time:new Date().toISOString()}, "大家正在看这张图片…");
  }

  function showCreateGroup() {
    CM.openDrawer("创建群聊", "选择 2～4 个已经存在的人物");
    CM.dom.drawerBody.innerHTML = `<div class="group-create-form"><label>群名称<input type="text" data-group-name maxlength="80" value="新群聊"></label><div><strong>选择成员</strong><div class="group-member-picker">${CM.state.characters.map(profile => `<label class="group-member-option"><input type="checkbox" data-group-member value="${CM.escapeHtml(profile.id)}"><span>${CM.escapeHtml(profile.name)} · ${CM.escapeHtml(profile.identity || profile.tagline || "")}</span></label>`).join("")}</div></div><div class="group-create-error error hidden" data-group-create-error></div><div class="group-create-actions"><button type="button" data-group-create-cancel>取消</button><button type="button" class="primary" data-group-create-confirm>创建群聊</button></div></div>`;
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
    CM.openDrawer(`${group.name} · 本轮反应`, "每个人独立决定发言或保持沉默；只展示开发者安全摘要");
    CM.dom.drawerBody.innerHTML = "<p>正在读取群成员反应…</p>";
    try {
      const data = await CM.api(`/v1/groups/${encodeURIComponent(group.id)}/turns/${encodeURIComponent(turnId)}/traces`);
      const traces = data.traces || [];
      CM.dom.drawerBody.innerHTML = traces.length ? `<div class="card-list">${traces.map(item => {
        const actions = item.actions || [];
        const actionText = actions.length ? actions.map(action => action.type).join(" / ") : "SILENCE";
        return `<div class="card"><strong>${CM.escapeHtml(item.character_name)}</strong><span> · ${CM.escapeHtml(actionText)} · ${CM.escapeHtml(CM.fmtMs(item.total_ms))}</span><div><b>注意：</b>${CM.escapeHtml(CM.hiddenIfEmpty(item.perception))}</div><div><b>反应：</b>${CM.escapeHtml(CM.hiddenIfEmpty(item.reaction))}</div>${item.created_memory_ids?.length ? `<div>Memory: ${CM.escapeHtml(item.created_memory_ids.join(", "))}</div>` : ""}</div>`;
      }).join("")}</div>` : '<p class="muted">本轮还没有角色判断记录。</p>';
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  }

  createButton?.addEventListener("click", showCreateGroup);
  list?.addEventListener("click", event => {
    const button = event.target.closest("[data-group]");
    if (button) enter(button.dataset.group).catch(console.error);
  });
  CM.dom.drawerBody.addEventListener("click", event => {
    if (event.target.closest("[data-group-create-cancel]")) CM.closeDrawer();
    if (event.target.closest("[data-group-create-confirm]")) createGroupFromDrawer().catch(console.error);
  });
  CM.dom.chat.addEventListener("click", event => {
    if (!CM.isGroupConversation()) return;
    const button = event.target.closest("[data-group-turn]");
    if (button) showTurn(button.dataset.groupTurn).catch(console.error);
  });

  const feature = CM.registerFeature("groups", {loadGroups,loadHistory,enter,leave,applyHeader,applyComposerState,sendText,sendSticker,sendImage,renderList});
  CM.on("ready", loadGroups);
})();
