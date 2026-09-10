let groups = [];
let groupMode = false;
let activeGroupId = null;
const pendingGroups = new Set();

const sidebar = document.querySelector(".sidebar");
const sidebarFoot = document.querySelector(".sidebar-foot");
const groupSection = document.createElement("section");
groupSection.className = "group-section";
groupSection.innerHTML = `
  <div class="group-section-head"><span>Groups</span><button class="group-create-button" type="button" title="创建群聊">＋</button></div>
  <nav class="group-list" aria-label="Groups"></nav>`;
sidebar?.insertBefore(groupSection, sidebarFoot || null);
const groupList = groupSection.querySelector(".group-list");
const groupCreateButton = groupSection.querySelector(".group-create-button");

function currentGroup() {
  return groups.find(item => item.id === activeGroupId) || null;
}

function groupInitial(group) {
  return (group?.name || "群").trim().slice(0, 1).toUpperCase();
}

function renderGroupList() {
  if (!groupList) return;
  if (!groups.length) {
    groupList.innerHTML = '<div class="group-members" style="padding:6px 12px">还没有群聊</div>';
    return;
  }
  groupList.innerHTML = groups.map(group => {
    const memberNames = (group.members || []).map(item => item.name || item.id).join("、");
    return `<button class="group-item ${groupMode && group.id === activeGroupId ? "active" : ""}" type="button" data-group="${escapeHtml(group.id)}">
      <span class="group-avatar">${escapeHtml(groupInitial(group))}</span>
      <span class="group-copy"><span class="group-name">${escapeHtml(group.name)}</span><span class="group-members">${escapeHtml(memberNames)}</span></span>
    </button>`;
  }).join("");
}

async function loadGroups() {
  const data = await api("/v1/groups");
  groups = data.groups || [];
  renderGroupList();
}

function groupMessageAvatar(message) {
  if (message.role === "user") return "";
  return String(message.actor_name || message.actor_id || "AI").trim().slice(0, 1).toUpperCase();
}

function addGroupMessage(message) {
  const row = document.createElement("article");
  row.className = `message-row ${message.role}${message.role === "assistant" ? " group-assistant" : ""}`;
  row.dataset.messageId = message.id ?? "";
  const speaker = message.role === "assistant" ? `<div class="group-speaker-name">${escapeHtml(message.actor_name || message.actor_id)}</div>` : "";
  const avatar = groupMessageAvatar(message);
  const hideStoredResourceText = (message.action === "STICKER" && message.sticker) || (message.action === "IMAGE" && message.image);
  const text = hideStoredResourceText ? "" : String(message.content || "").trim();
  const textHtml = text ? `<div class="bubble">${escapeHtml(text)}</div>` : "";
  const stickerHtml = message.sticker?.url
    ? `<div class="sticker-bubble"><img class="group-message-sticker" src="${escapeHtml(message.sticker.url)}" alt="${escapeHtml(message.sticker.label || "表情包")}" loading="lazy"><span class="sticker-fallback">表情</span></div>`
    : "";
  const imageHtml = message.image?.url
    ? `<div class="image-bubble"><img class="group-message-image" src="${escapeHtml(message.image.url)}" alt="${escapeHtml(message.image.label || "图片")}" loading="lazy"><div class="image-caption">${escapeHtml(message.image.label || "图片")}</div></div>`
    : "";
  const turnDebug = message.role === "user" && message.turn_id
    ? `<button class="group-turn-debug" type="button" data-group-turn="${escapeHtml(message.turn_id)}">本轮反应</button>`
    : "";
  row.innerHTML = `<div class="avatar">${escapeHtml(avatar)}</div><div class="bubble-wrap">${speaker}${textHtml}${stickerHtml}${imageHtml}<div class="message-meta"><span>${fmtTime(message.event_time)}</span>${turnDebug}</div></div>`;
  row.querySelectorAll(".sticker-bubble img").forEach(img => {
    img.addEventListener("error", () => img.closest(".sticker-bubble")?.classList.add("broken"), {once:true});
  });
  chat.appendChild(row);
}

function renderGroupHistory(messages) {
  chat.innerHTML = "";
  const group = currentGroup();
  if (!messages.length) {
    chat.innerHTML = `<div class="empty">「${escapeHtml(group?.name || "群聊")}」还没有消息。<br>说第一句话，看看谁会接话。</div>`;
    return;
  }
  let lastDate = null;
  for (const message of messages) {
    const date = fmtDate(message.event_time);
    if (date !== lastDate) {
      const sep = document.createElement("div");
      sep.className = "date-separator";
      sep.textContent = `── ${date} ──`;
      chat.appendChild(sep);
      lastDate = date;
    }
    addGroupMessage(message);
  }
  if (pendingGroups.has(activeGroupId)) {
    const pending = document.createElement("div");
    pending.className = "group-pending-note";
    pending.textContent = "大家正在看这条消息…";
    chat.appendChild(pending);
  }
  scrollToBottom(false);
}

async function loadGroupHistory() {
  const requested = activeGroupId;
  if (!requested) return;
  const data = await api(`/v1/groups/${encodeURIComponent(requested)}/history?limit=180`);
  if (!groupMode || requested !== activeGroupId) return;
  const index = groups.findIndex(item => item.id === requested);
  if (index >= 0 && data.group) groups[index] = data.group;
  renderGroupList();
  renderGroupHistory(data.messages || []);
}

async function switchGroup(groupId) {
  const group = groups.find(item => item.id === groupId);
  if (!group) return;
  groupMode = true;
  activeGroupId = groupId;
  document.body.classList.add("group-mode");
  closeDrawer();
  if (typeof closeStickerPanel === "function") closeStickerPanel();
  if (typeof closeImagePanel === "function") closeImagePanel();
  renderGroupList();
  updateHeader();
  chat.innerHTML = '<div class="empty">正在加载群聊记录…</div>';
  await loadGroupHistory();
  updateComposerState();
}

const p011DirectSwitchCharacter = switchCharacter;
switchCharacter = async function switchCharacterFromGroup(nextId) {
  groupMode = false;
  activeGroupId = null;
  document.body.classList.remove("group-mode");
  renderGroupList();
  return p011DirectSwitchCharacter(nextId);
};

const p011DirectUpdateHeader = updateHeader;
updateHeader = function updateHeaderWithGroup() {
  if (!groupMode) return p011DirectUpdateHeader();
  const group = currentGroup();
  if (!group) return;
  const memberNames = (group.members || []).map(item => item.name || item.id).join("、");
  const pending = pendingGroups.has(group.id);
  characterName.textContent = group.name;
  characterIdentity.textContent = `${memberNames}${pending ? " · 大家正在回复" : ""}`;
  headerAvatar.textContent = groupInitial(group);
  input.placeholder = pending ? "群成员正在回复…" : `发到「${group.name}」`;
  runtimeButton.disabled = true;
  runtimeButton.title = "群聊请使用每轮反应 Inspector";
  if (typeof intentButton !== "undefined") intentButton.disabled = true;
  updateComposerState();
};

const p011DirectComposerState = updateComposerState;
updateComposerState = function updateComposerStateWithGroup() {
  if (!groupMode) {
    runtimeButton.disabled = false;
    runtimeButton.title = "";
    if (typeof intentButton !== "undefined") intentButton.disabled = false;
    const stickerTrigger = document.querySelector(".sticker-trigger");
    if (stickerTrigger) stickerTrigger.disabled = false;
    return p011DirectComposerState();
  }
  const pending = pendingGroups.has(activeGroupId);
  sendButton.disabled = pending;
  input.disabled = pending;
  const stickerTrigger = document.querySelector(".sticker-trigger");
  if (stickerTrigger) {
    stickerTrigger.disabled = true;
    stickerTrigger.title = "P0.11 V0 暂不支持用户群聊表情包";
  }
  if (!pending) input.focus();
};

const p011DirectLoadHistory = loadHistory;
loadHistory = async function loadHistoryWithGroup() {
  if (groupMode) return loadGroupHistory();
  return p011DirectLoadHistory();
};

function showCreateGroup() {
  openDrawer("创建群聊", "选择 2～4 个已经存在的人物");
  drawerBody.innerHTML = `<div class="group-create-form">
    <label>群名称<input type="text" data-group-name maxlength="80" value="新群聊"></label>
    <div><strong>选择成员</strong><div class="group-member-picker">${characters.map(profile => `<label class="group-member-option"><input type="checkbox" data-group-member value="${escapeHtml(profile.id)}"><span>${escapeHtml(profile.name)} · ${escapeHtml(profile.identity || profile.tagline || "")}</span></label>`).join("")}</div></div>
    <div class="group-create-error error hidden" data-group-create-error></div>
    <div class="group-create-actions"><button type="button" data-group-create-cancel>取消</button><button type="button" class="primary" data-group-create-confirm>创建群聊</button></div>
  </div>`;
}

async function createGroupFromDrawer() {
  const name = drawerBody.querySelector("[data-group-name]")?.value.trim() || "新群聊";
  const memberIds = [...drawerBody.querySelectorAll("[data-group-member]:checked")].map(input => input.value);
  const errorBox = drawerBody.querySelector("[data-group-create-error]");
  if (memberIds.length < 2 || memberIds.length > 4) {
    if (errorBox) { errorBox.textContent = "请选择 2～4 个群成员。"; errorBox.classList.remove("hidden"); }
    return;
  }
  try {
    const data = await api("/v1/groups", {method:"POST", body:JSON.stringify({name, member_ids:memberIds})});
    groups.unshift(data.group);
    closeDrawer();
    renderGroupList();
    await switchGroup(data.group.id);
  } catch (error) {
    if (errorBox) { errorBox.textContent = error.message; errorBox.classList.remove("hidden"); }
  }
}

groupCreateButton?.addEventListener("click", showCreateGroup);
groupList?.addEventListener("click", event => {
  const button = event.target.closest("[data-group]");
  if (button) switchGroup(button.dataset.group).catch(console.error);
});
drawerBody.addEventListener("click", event => {
  if (event.target.closest("[data-group-create-cancel]")) closeDrawer();
  if (event.target.closest("[data-group-create-confirm]")) createGroupFromDrawer().catch(console.error);
});

async function showGroupTurn(turnId) {
  const group = currentGroup();
  if (!group) return;
  openDrawer(`${group.name} · 本轮反应`, "每个人独立决定发言或保持沉默；只展示开发者安全摘要");
  drawerBody.innerHTML = "<p>正在读取群成员反应…</p>";
  try {
    const data = await api(`/v1/groups/${encodeURIComponent(group.id)}/turns/${encodeURIComponent(turnId)}/traces`);
    const traces = data.traces || [];
    drawerBody.innerHTML = traces.length ? `<div class="card-list">${traces.map(item => {
      const actions = item.actions || [];
      const actionText = actions.length ? actions.map(action => action.type).join(" / ") : "SILENCE";
      return `<div class="card"><strong>${escapeHtml(item.character_name)}</strong><span> · ${escapeHtml(actionText)} · ${escapeHtml(fmtMs(item.total_ms))}</span><div><b>注意：</b>${escapeHtml(hiddenIfEmpty(item.perception))}</div><div><b>反应：</b>${escapeHtml(hiddenIfEmpty(item.reaction))}</div>${item.created_memory_ids?.length ? `<div>Memory: ${escapeHtml(item.created_memory_ids.join(", "))}</div>` : ""}</div>`;
    }).join("")}</div>` : '<p class="muted">本轮还没有角色判断记录。</p>';
  } catch (error) {
    drawerBody.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
  }
}

chat.addEventListener("click", event => {
  if (!groupMode) return;
  const button = event.target.closest("[data-group-turn]");
  if (button) showGroupTurn(button.dataset.groupTurn).catch(console.error);
});

async function sendGroupText(message) {
  const sentGroup = activeGroupId;
  if (!sentGroup || pendingGroups.has(sentGroup)) return;
  pendingGroups.add(sentGroup);
  updateHeader();
  if (chat.querySelector(".empty")) chat.innerHTML = "";
  addGroupMessage({role:"user", actor_type:"USER", actor_id:"user", actor_name:"我", content:message, event_time:new Date().toISOString()});
  const pending = document.createElement("div");
  pending.className = "group-pending-note";
  pending.textContent = "大家正在看这条消息…";
  chat.appendChild(pending);
  input.value = "";
  input.style.height = "auto";
  scrollToBottom();
  try {
    const result = await api(`/v1/groups/${encodeURIComponent(sentGroup)}/chat`, {method:"POST", body:JSON.stringify({message})});
    if (groupMode && sentGroup === activeGroupId) {
      const index = groups.findIndex(item => item.id === sentGroup);
      if (index >= 0 && result.group) groups[index] = result.group;
      renderGroupHistory(result.messages || []);
      renderGroupList();
    }
  } catch (error) {
    console.error("[group chat failed]", sentGroup, error);
    if (groupMode && sentGroup === activeGroupId) {
      chat.querySelector(".group-pending-note")?.remove();
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `群聊生成失败：${error.message}`;
      chat.appendChild(box);
    }
  } finally {
    pendingGroups.delete(sentGroup);
    updateHeader();
    if (groupMode && sentGroup === activeGroupId) await loadGroupHistory().catch(console.warn);
  }
}

// Capture the submit before the legacy 1:1 bubble handler. Direct chat is untouched.
composer.addEventListener("submit", event => {
  if (!groupMode) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  const message = input.value.trim();
  if (message) sendGroupText(message).catch(console.error);
}, true);

if (typeof sendImageDraft === "function") {
  const p011DirectSendImageDraft = sendImageDraft;
  sendImageDraft = async function sendImageDraftWithGroup() {
    if (!groupMode) return p011DirectSendImageDraft();
    const draft = imageDraft;
    const sentGroup = activeGroupId;
    if (!draft || !sentGroup || pendingGroups.has(sentGroup)) return;
    const caption = document.getElementById("imageCaption")?.value.trim() || "";
    pendingGroups.add(sentGroup);
    updateHeader();
    if (chat.querySelector(".empty")) chat.innerHTML = "";
    addGroupMessage({role:"user", actor_type:"USER", actor_id:"user", actor_name:"我", content:caption, image:{url:draft.data_url,label:draft.filename}, event_time:new Date().toISOString()});
    closeImagePanel();
    const pending = document.createElement("div");
    pending.className = "group-pending-note";
    pending.textContent = "大家正在看这张图片…";
    chat.appendChild(pending);
    scrollToBottom();
    try {
      const result = await api(`/v1/groups/${encodeURIComponent(sentGroup)}/chat`, {
        method:"POST",
        body:JSON.stringify({message:caption, image:{filename:draft.filename, data_url:draft.data_url}}),
      });
      if (groupMode && sentGroup === activeGroupId) {
        const index = groups.findIndex(item => item.id === sentGroup);
        if (index >= 0 && result.group) groups[index] = result.group;
        renderGroupHistory(result.messages || []);
        renderGroupList();
      }
    } catch (error) {
      if (groupMode && sentGroup === activeGroupId) {
        chat.querySelector(".group-pending-note")?.remove();
        const box = document.createElement("div");
        box.className = "error";
        box.textContent = `群聊图片发送失败：${error.message}`;
        chat.appendChild(box);
      }
    } finally {
      pendingGroups.delete(sentGroup);
      updateHeader();
      if (groupMode && sentGroup === activeGroupId) await loadGroupHistory().catch(console.warn);
    }
  };
}

loadGroups().catch(error => console.warn("group list load failed", error));
