let imageDraft = null;
let imagePanel = null;
let imageInput = null;

visibleActions = function visibleActionsWithImages(result) {
  const actions = Array.isArray(result?.actions) ? result.actions : [];
  if (actions.length) {
    return actions.filter(action => {
      if (action?.type === "STICKER") return Boolean(action?.sticker_id);
      if (action?.type === "IMAGE") return Boolean(action?.image_id);
      return Boolean(String(action?.message || "").trim());
    });
  }
  const action = result?.action;
  if (!action) return [];
  if (action.type === "STICKER" && action.sticker_id) return [action];
  if (action.type === "IMAGE" && action.image_id) return [action];
  return action.message ? [action] : [];
};

addMessage = function addMessageWithMedia(message) {
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

  const messageCharacter = message.character_id || characterId;
  const sticker = message.sticker || (message.sticker_id ? {
    id: message.sticker_id,
    label: message.sticker_label || "表情包",
    url: stickerAsset(messageCharacter, message.sticker_id),
  } : null);
  const image = message.image || null;
  const hideStoredResourceText = (message.action === "STICKER" && sticker) || (message.action === "IMAGE" && image);
  const text = hideStoredResourceText ? "" : String(message.content || "").trim();
  const textHtml = text ? `<div class="bubble">${escapeHtml(text)}</div>` : "";
  const stickerUrl = sticker?.url || (sticker?.id ? stickerAsset(messageCharacter, sticker.id) : "");
  const stickerHtml = stickerUrl
    ? `<div class="sticker-bubble"><img src="${escapeHtml(stickerUrl)}" alt="${escapeHtml(sticker.label || "表情包")}" loading="lazy"><span class="sticker-fallback">表情</span></div>`
    : "";
  const imageHtml = image?.url
    ? `<div class="image-bubble"><img src="${escapeHtml(image.url)}" alt="${escapeHtml(image.label || "图片")}" loading="lazy"><div class="image-caption">${escapeHtml(image.label || "图片")}</div></div>`
    : "";
  const proactive = message.proactive || message.action === "PROACTIVE_MESSAGE";
  row.innerHTML = `<div class="avatar">${escapeHtml(avatar)}</div><div class="bubble-wrap">${textHtml}${stickerHtml}${imageHtml}<div class="message-meta"><span>${fmtTime(message.event_time)}</span>${latency}${proactive ? "<span>主动消息</span>" : ""}${thought}${trace}</div></div>`;
  row.querySelectorAll(".sticker-bubble img").forEach(img => {
    img.addEventListener("error", () => img.closest(".sticker-bubble")?.classList.add("broken"), {once:true});
  });
  chat.appendChild(row);
  return row;
};

deliveryDelay = function deliveryDelayWithImages(action) {
  const type = String(action?.type || "MESSAGE");
  const text = String(action?.message || "");
  if (type === "EMOJI" || type === "STICKER" || type === "IMAGE") return 180 + Math.floor(Math.random() * 260);
  const base = 300 + Math.min(text.length * 14, 700);
  const jitter = Math.floor(Math.random() * 260) - 80;
  return Math.max(260, Math.min(base + jitter, 1200));
};

revealActionsWithRhythm = async function revealActionsWithImageRhythm(result, sentCharacter, browserTotal) {
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
      character_id: sentCharacter,
      content: action.message || "",
      sticker_id: action.sticker_id,
      sticker: action.sticker,
      image_id: action.image_id,
      image: action.image,
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
  previewFor = function previewForWithImages(profile) {
    const summary = characterSummaries.get(profile.id);
    const latest = summary?.latest_message;
    if (!latest) return profile.tagline || profile.identity || "Persistent AI Person";
    const mediaFallback = latest.image ? `[图片] ${latest.image.label || "图片"}` : (latest.sticker ? `[表情包] ${latest.sticker.label}` : "");
    const body = latest.preview || latest.content || mediaFallback;
    if (!body) return profile.tagline || profile.identity || "Persistent AI Person";
    return `${latest.role === "user" ? "你：" : ""}${body}`;
  };
}

function closeImagePanel() {
  if (imagePanel) imagePanel.classList.add("hidden");
  imageDraft = null;
  if (imageInput) imageInput.value = "";
}

function openImageDraft(file, {source = "FILE_PICKER"} = {}) {
  if (!imagePanel || !file) return;
  const allowed = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);
  if (!allowed.has(file.type)) {
    imagePanel.classList.remove("hidden");
    imagePanel.innerHTML = '<div class="error">只支持 JPEG / PNG / GIF / WebP。</div>';
    return;
  }
  if (file.size > 8 * 1024 * 1024) {
    imagePanel.classList.remove("hidden");
    imagePanel.innerHTML = '<div class="error">图片不能超过 8 MiB。</div>';
    return;
  }

  const reader = new FileReader();
  reader.onload = () => {
    const defaultName = source === "CLIPBOARD" ? `clipboard-${Date.now()}.png` : "image";
    imageDraft = {filename: file.name || defaultName, data_url: String(reader.result || ""), size: file.size, source};
    closeStickerPanel?.();
    imagePanel.classList.remove("hidden");
    imagePanel.innerHTML = `
      <div class="image-draft-preview"><img src="${escapeHtml(imageDraft.data_url)}" alt="图片预览"></div>
      <div class="image-draft-meta">${source === "CLIPBOARD" ? "来自剪贴板" : escapeHtml(imageDraft.filename)} · ${(file.size / 1024).toFixed(0)} KB</div>
      <textarea id="imageCaption" rows="2" maxlength="12000" placeholder="可以补一句话，也可以只发图片"></textarea>
      <div class="image-draft-actions"><button type="button" data-image-cancel>取消</button><button class="image-send" type="button" data-image-send>发送图片</button></div>`;
    document.getElementById("imageCaption")?.focus();
  };
  reader.onerror = () => {
    imagePanel.classList.remove("hidden");
    imagePanel.innerHTML = '<div class="error">图片读取失败。</div>';
  };
  reader.readAsDataURL(file);
}

async function sendImageDraft() {
  const draft = imageDraft;
  const sentCharacter = characterId;
  if (!draft || pendingCharacters.has(sentCharacter)) return;
  const caption = document.getElementById("imageCaption")?.value.trim() || "";
  const conversationId = conversationIdFor(sentCharacter);
  const browserStarted = performance.now();
  pendingCharacters.add(sentCharacter);
  renderCharacterList();
  updateHeader();

  if (chat.querySelector(".empty")) chat.innerHTML = "";
  addMessage({
    role: "user",
    character_id: sentCharacter,
    content: caption,
    image: {url: draft.data_url, label: draft.filename, source: draft.source || "LOCAL_PREVIEW"},
    event_time: new Date().toISOString(),
    has_trace: false,
  });
  closeImagePanel();
  appendTypingForCurrent();
  scrollToBottom();

  try {
    const result = await api("/v1/chat", {
      method: "POST",
      body: JSON.stringify({
        character_id: sentCharacter,
        conversation_id: conversationId,
        message: caption,
        image: {filename: draft.filename, data_url: draft.data_url},
      }),
    });
    const browserTotal = performance.now() - browserStarted;
    console.info("[chat image timings]", sentCharacter, {...(result.timings || {}), browser_total_ms:Number(browserTotal.toFixed(1))});
    if (sentCharacter === characterId) {
      chat.querySelector(".typing-row")?.remove();
      await revealActionsWithRhythm(result, sentCharacter, browserTotal);
    }
  } catch (error) {
    console.error("[image chat failed]", sentCharacter, error);
    if (sentCharacter === characterId) {
      chat.querySelector(".typing-row")?.remove();
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `发送图片失败：${error.message}`;
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

const imageButton = document.createElement("button");
imageButton.type = "button";
imageButton.className = "image-trigger";
imageButton.textContent = "▧";
imageButton.title = "发送图片";
composer.insertBefore(imageButton, input);

imageInput = document.createElement("input");
imageInput.type = "file";
imageInput.accept = "image/jpeg,image/png,image/gif,image/webp";
imageInput.className = "image-file-input";
document.body.appendChild(imageInput);

imagePanel = document.createElement("div");
imagePanel.className = "image-panel hidden";
document.querySelector(".composer-wrap")?.appendChild(imagePanel);

imageButton.addEventListener("click", event => {
  event.stopPropagation();
  closeStickerPanel?.();
  imageInput.click();
});
imageInput.addEventListener("change", () => openImageDraft(imageInput.files?.[0], {source:"FILE_PICKER"}));

input.addEventListener("paste", event => {
  const items = [...(event.clipboardData?.items || [])];
  const imageItem = items.find(item => item.kind === "file" && String(item.type || "").startsWith("image/"));
  if (!imageItem) return;
  const file = imageItem.getAsFile();
  if (!file) return;
  event.preventDefault();
  openImageDraft(file, {source:"CLIPBOARD"});
});

imagePanel.addEventListener("click", event => {
  if (event.target.closest("[data-image-cancel]")) closeImagePanel();
  if (event.target.closest("[data-image-send]")) sendImageDraft().catch(console.error);
});
document.addEventListener("click", event => {
  if (!event.target.closest(".image-panel") && !event.target.closest(".image-trigger")) {
    if (!imagePanel?.contains(document.activeElement)) closeImagePanel();
  }
});

const p08SwitchCharacter = switchCharacter;
switchCharacter = async function switchCharacterWithImageReset(nextId) {
  closeImagePanel();
  return p08SwitchCharacter(nextId);
};
