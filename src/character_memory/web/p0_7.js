const stickerCache = new Map();
let stickerPanel = null;

function stickerAsset(character, stickerId) {
  return `/v1/stickers/${encodeURIComponent(character)}/${encodeURIComponent(stickerId)}/asset`;
}

visibleActions = function visibleActionsWithStickers(result) {
  const actions = Array.isArray(result?.actions) ? result.actions : [];
  if (actions.length) {
    return actions.filter(action => action?.type === "STICKER" ? Boolean(action?.sticker_id) : Boolean(String(action?.message || "").trim()));
  }
  const action = result?.action;
  if (!action) return [];
  if (action.type === "STICKER" && action.sticker_id) return [action];
  return action.message ? [action] : [];
};

addMessage = function addMessageWithSticker(message) {
  const row = document.createElement("article");
  row.className = `message-row ${message.role}`;
  row.dataset.messageId = message.id ?? "";
  const latency = message.latency_ms ? `<span>耗时 ${fmtMs(message.latency_ms)}</span>` : "";
  const avatar = message.role === "assistant" ? initialFor(currentProfile()) : "";
  const thought = message.role === "assistant" && message.has_trace && message.source_event_id
    ? `<button class="detail-button" type="button" data-thought="${message.source_event_id}" title="查看安全的思考摘要">想法</button>`
    : "";
  const trace = message.has_trace && message.source_event_id
    ? `<button class="detail-button" type="button" data-trace="${message.source_event_id}" title="查看本轮开发详情">···</button>`
    : "";
  const sticker = message.sticker || (message.sticker_id ? {
    id: message.sticker_id,
    label: message.sticker_label || "表情包",
    url: stickerAsset(characterId, message.sticker_id),
  } : null);
  const hideStoredStickerText = message.action === "STICKER" && sticker;
  const text = hideStoredStickerText ? "" : String(message.content || "").trim();
  const textHtml = text ? `<div class="bubble">${escapeHtml(text)}</div>` : "";
  const stickerHtml = sticker?.url
    ? `<div class="sticker-bubble"><img src="${escapeHtml(sticker.url)}" alt="${escapeHtml(sticker.label || "表情包")}" loading="lazy"></div>`
    : "";
  const proactive = message.proactive || message.action === "PROACTIVE_MESSAGE";
  row.innerHTML = `<div class="avatar">${escapeHtml(avatar)}</div><div class="bubble-wrap">${textHtml}${stickerHtml}<div class="message-meta"><span>${fmtTime(message.event_time)}</span>${latency}${proactive ? "<span>主动消息</span>" : ""}${thought}${trace}</div></div>`;
  chat.appendChild(row);
  return row;
};

deliveryDelay = function deliveryDelayWithSticker(action) {
  const type = String(action?.type || "MESSAGE");
  const text = String(action?.message || "");
  if (type === "EMOJI" || type === "STICKER") return 180 + Math.floor(Math.random() * 220);
  const base = 300 + Math.min(text.length * 14, 700);
  const jitter = Math.floor(Math.random() * 260) - 80;
  return Math.max(260, Math.min(base + jitter, 1200));
};

revealActionsWithRhythm = async function revealActionsWithStickerRhythm(result, sentCharacter, browserTotal) {
  const actions = visibleActions(result);
  if (!actions.length) {
    const note = document.createElement("div");
    note.className = "date-separator quiet-read";
    note.textContent = `已读 · 没有回复 · ${fmtMs(browserTotal)}`;
    chat.appendChild(note);
    return;
  }

  for (let index = 0; index < actions.length; index += 1) {
    if (sentCharacter !== characterId) return;
    if (index > 0) {
      appendTypingForCurrent();
      scrollToBottom();
      await sleep(deliveryDelay(actions[index]));
      if (sentCharacter !== characterId) return;
      chat.querySelector(".typing-row")?.remove();
    }
    const action = actions[index];
    addMessage({
      role: "assistant",
      content: action.message || "",
      sticker_id: action.sticker_id,
      sticker: action.sticker,
      event_time: result.event_time,
      action: action.type,
      source_event_id: result.event_id,
      has_trace: true,
      latency_ms: index === 0 ? browserTotal : 0,
    });
    scrollToBottom();
  }
};

if (typeof previewFor === "function") {
  previewFor = function previewForWithStickers(profile) {
    const summary = characterSummaries.get(profile.id);
    const latest = summary?.latest_message;
    if (!latest) return profile.tagline || profile.identity || "Persistent AI Person";
    const body = latest.preview || latest.content || (latest.sticker ? `[表情包] ${latest.sticker.label}` : "");
    if (!body) return profile.tagline || profile.identity || "Persistent AI Person";
    return `${latest.role === "user" ? "你：" : ""}${body}`;
  };
}

async function loadStickers(character = characterId) {
  if (stickerCache.has(character)) return stickerCache.get(character);
  const data = await api(`/v1/stickers?character_id=${encodeURIComponent(character)}`);
  const values = data.stickers || [];
  stickerCache.set(character, values);
  return values;
}

function closeStickerPanel() {
  if (stickerPanel) stickerPanel.classList.add("hidden");
}

async function openStickerPanel() {
  if (!stickerPanel) return;
  stickerPanel.classList.remove("hidden");
  stickerPanel.innerHTML = '<div class="sticker-loading">正在拿表情包…</div>';
  try {
    const requested = characterId;
    const stickers = await loadStickers(requested);
    if (requested !== characterId) return closeStickerPanel();
    stickerPanel.innerHTML = stickers.length
      ? `<div class="sticker-grid">${stickers.map(item => `<button type="button" class="sticker-choice" data-sticker-id="${escapeHtml(item.id)}" title="${escapeHtml(item.label)}"><img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.label)}"><span>${escapeHtml(item.label)}</span></button>`).join("")}</div>`
      : '<div class="sticker-loading">这个人物还没有可用表情包。</div>';
  } catch (error) {
    stickerPanel.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
  }
}

async function sendSticker(sticker) {
  const sentCharacter = characterId;
  if (!sticker || pendingCharacters.has(sentCharacter)) return;
  closeStickerPanel();
  const conversationId = conversationIdFor(sentCharacter);
  const browserStarted = performance.now();
  pendingCharacters.add(sentCharacter);
  renderCharacterList();
  updateHeader();

  if (chat.querySelector(".empty")) chat.innerHTML = "";
  addMessage({role:"user", content:"", sticker, sticker_id:sticker.id, event_time:new Date().toISOString(), has_trace:false});
  appendTypingForCurrent();
  scrollToBottom();

  try {
    const result = await api("/v1/chat", {method:"POST", body:JSON.stringify({character_id:sentCharacter, conversation_id:conversationId, message:"", sticker_id:sticker.id})});
    const browserTotal = performance.now() - browserStarted;
    console.info("[chat sticker timings]", sentCharacter, {...(result.timings || {}), browser_total_ms:Number(browserTotal.toFixed(1))});
    if (sentCharacter === characterId) {
      chat.querySelector(".typing-row")?.remove();
      await revealActionsWithRhythm(result, sentCharacter, browserTotal);
    }
  } catch (error) {
    console.error("[sticker chat failed]", sentCharacter, error);
    if (sentCharacter === characterId) {
      chat.querySelector(".typing-row")?.remove();
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `发送表情包失败：${error.message}`;
      chat.appendChild(box);
    }
  } finally {
    pendingCharacters.delete(sentCharacter);
    renderCharacterList();
    updateHeader();
    if (sentCharacter === characterId) {
      await loadHistory().catch(error => console.warn("history refresh failed", error));
      updateComposerState();
      scrollToBottom();
    }
  }
}

const stickerButton = document.createElement("button");
stickerButton.type = "button";
stickerButton.className = "sticker-trigger";
stickerButton.textContent = "☺";
stickerButton.title = "发送表情包";
composer.insertBefore(stickerButton, input);
stickerPanel = document.createElement("div");
stickerPanel.className = "sticker-panel hidden";
document.querySelector(".composer-wrap")?.appendChild(stickerPanel);

stickerButton.addEventListener("click", event => {
  event.stopPropagation();
  if (stickerPanel.classList.contains("hidden")) openStickerPanel(); else closeStickerPanel();
});
stickerPanel.addEventListener("click", async event => {
  const button = event.target.closest("[data-sticker-id]");
  if (!button) return;
  const stickers = await loadStickers(characterId);
  const sticker = stickers.find(item => item.id === button.dataset.stickerId);
  await sendSticker(sticker);
});
document.addEventListener("click", event => {
  if (!event.target.closest(".sticker-panel") && !event.target.closest(".sticker-trigger")) closeStickerPanel();
});

function formatIntentTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function intentStatusText(status) {
  return ({PENDING:"等待中", PROCESSING:"执行中", EXECUTED:"已执行", SUPPRESSED:"已放弃", DEFERRED:"已延后", EXPIRED:"已过期", ERROR:"执行失败"})[status] || status || "未知";
}

function intentCards(intents) {
  if (!intents.length) return '<p class="muted">这个人物还没有产生过 Intent。也就是说，目前并没有“以后想再做/再问某件事”的计划。</p>';
  return `<div class="intent-list">${intents.map(item => `<article class="intent-card intent-${escapeHtml(String(item.status || "").toLowerCase())}"><div class="intent-head"><span class="intent-status">${escapeHtml(intentStatusText(item.status))}</span><span>#${escapeHtml(item.id)}</span></div><div class="intent-content">${escapeHtml(item.content)}</div><div class="intent-times"><span>产生：${escapeHtml(formatIntentTime(item.created_at))}</span><span>最早：${escapeHtml(formatIntentTime(item.earliest_at))}</span><span>过期：${escapeHtml(formatIntentTime(item.expires_at))}</span></div>${item.source_event_id ? `<button type="button" class="intent-source" data-intent-source="${escapeHtml(item.source_event_id)}">查看产生它的聊天轮次 · Event #${escapeHtml(item.source_event_id)}</button>` : ""}${item.reason ? `<div class="intent-reason">当时原因：${escapeHtml(item.reason)}</div>` : ""}</article>`).join("")}</div>`;
}

async function showIntentPreview() {
  const requestedCharacter = characterId;
  openDrawer(`${currentProfile().name} · Intent`, "模型在聊天时产生的未来行动意图；这里是调试预览，不向人物泄露");
  drawerBody.innerHTML = "<p>正在读取 Intent…</p>";
  try {
    const data = await api(`/v1/runtime/${encodeURIComponent(requestedCharacter)}`);
    if (requestedCharacter !== characterId) return;
    const intents = data.intents || [];
    const pending = intents.filter(item => item.status === "PENDING").length;
    drawerBody.innerHTML = `<section class="section"><h3>Intent Preview</h3><div class="intent-summary"><strong>${pending}</strong> 个等待中的 Intent · 最近共 ${intents.length} 条</div>${intentCards(intents)}</section><section class="section"><h3>怎么判断问题在哪</h3><p class="muted">完全没有 Intent：通常是模型没有形成未来行动意图。PENDING：还没到时间或仍在等待用户回复。SUPPRESSED：到期后人物再次判断，决定不发送。EXECUTED：已经形成主动消息。</p></section>`;
  } catch (error) {
    drawerBody.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
  }
}

const intentButton = document.createElement("button");
intentButton.type = "button";
intentButton.className = "ghost-button";
intentButton.textContent = "Intent";
intentButton.title = "查看人物当前和历史 Intent";
runtimeButton.insertAdjacentElement("beforebegin", intentButton);
intentButton.addEventListener("click", showIntentPreview);

drawerBody.addEventListener("click", event => {
  const source = event.target.closest("[data-intent-source]");
  if (source) showTrace(source.dataset.intentSource);
});

const p07SwitchCharacter = switchCharacter;
switchCharacter = async function switchCharacterWithStickerReset(nextId) {
  closeStickerPanel();
  return p07SwitchCharacter(nextId);
};
