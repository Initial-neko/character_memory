(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before images.js");

  let draft = null;
  let panel = null;
  let inputEl = null;
  let trigger = null;
  let lightbox = null;
  let lightboxAvatarSource = null;

  function close() {
    panel?.classList.add("hidden");
    draft = null;
    if (inputEl) inputEl.value = "";
  }

  function setDisabled(disabled) {
    if (trigger) trigger.disabled = Boolean(disabled);
  }

  function avatarSourceFor(rawUrl) {
    if (!rawUrl || CM.isGroupConversation()) return null;
    let pathname = "";
    try { pathname = new URL(rawUrl, window.location.origin).pathname; } catch { return null; }
    let match = pathname.match(/^\/v1\/media\/([^/]+)$/);
    if (match) return {mediaId:decodeURIComponent(match[1]), characterId:CM.state.characterId};
    match = pathname.match(/^\/v1\/images\/([^/]+)\/([^/]+)\/asset$/);
    if (match && decodeURIComponent(match[1]) === CM.state.characterId) {
      return {imageId:decodeURIComponent(match[2]), characterId:CM.state.characterId};
    }
    return null;
  }

  function closeLightbox() {
    if (!lightbox) return;
    lightbox.classList.add("hidden");
    lightboxAvatarSource = null;
    const image = lightbox.querySelector("img");
    if (image) { image.removeAttribute("src"); image.alt = ""; }
  }

  function openLightbox(image) {
    if (!lightbox || !image?.src) return;
    const preview = lightbox.querySelector("img");
    if (!preview) return;
    preview.src = image.currentSrc || image.src;
    preview.alt = image.alt || "图片预览";
    lightboxAvatarSource = avatarSourceFor(preview.src);
    const avatarButton = lightbox.querySelector("[data-image-set-avatar]");
    if (avatarButton) {
      avatarButton.hidden = !lightboxAvatarSource;
      avatarButton.disabled = false;
      avatarButton.textContent = "设为当前头像";
    }
    lightbox.classList.remove("hidden");
  }

  function renderDraft(nextDraft) {
    if (!panel || !nextDraft?.data_url) return;
    draft = nextDraft;
    CM.features.stickers?.close?.();
    CM.features.aiImages?.close?.();
    panel.classList.remove("hidden");
    const sourceLabel = draft.source === "CLIPBOARD"
      ? "来自剪贴板"
      : draft.source === "AI_GENERATED"
        ? "来自 AI 生成"
        : CM.escapeHtml(draft.filename || "image");
    const sizeText = Number(draft.size || 0) > 0 ? ` · ${(Number(draft.size) / 1024).toFixed(0)} KB` : "";
    panel.innerHTML = `<div class="image-draft-preview"><img src="${CM.escapeHtml(draft.data_url)}" alt="图片预览"></div>
      <div class="image-draft-meta">${sourceLabel}${sizeText}</div>
      <textarea id="imageCaption" rows="2" maxlength="12000" placeholder="可以补一句话，也可以只发图片"></textarea>
      <div class="image-draft-actions"><button type="button" data-image-cancel>取消</button><button class="primary" type="button" data-image-send>发送图片</button></div>`;
    document.getElementById("imageCaption")?.focus();
  }

  function openDataDraft({filename = "image", data_url = "", size = 0, source = "AI_GENERATED"} = {}) {
    const value = String(data_url || "").trim();
    if (!/^data:image\/(?:jpeg|png|gif|webp);base64,/i.test(value)) {
      panel.classList.remove("hidden");
      panel.innerHTML = '<div class="error">生成结果不是可发送的 JPEG / PNG / GIF / WebP 图片。</div>';
      return;
    }
    if (Number(size || 0) > 8 * 1024 * 1024) {
      panel.classList.remove("hidden");
      panel.innerHTML = '<div class="error">图片不能超过 8 MiB。</div>';
      return;
    }
    renderDraft({filename:String(filename || "image"), data_url:value, size:Number(size || 0), source});
  }

  function openDraft(file, {source = "FILE_PICKER"} = {}) {
    if (!panel || !file) return;
    const allowed = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);
    if (!allowed.has(file.type)) {
      panel.classList.remove("hidden");
      panel.innerHTML = '<div class="error">只支持 JPEG / PNG / GIF / WebP。</div>';
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      panel.classList.remove("hidden");
      panel.innerHTML = '<div class="error">图片不能超过 8 MiB。</div>';
      return;
    }

    const reader = new FileReader();
    reader.onload = () => {
      const defaultName = source === "CLIPBOARD" ? `clipboard-${Date.now()}.png` : "image";
      openDataDraft({
        filename:file.name || defaultName,
        data_url:String(reader.result || ""),
        size:file.size,
        source,
      });
    };
    reader.onerror = () => {
      panel.classList.remove("hidden");
      panel.innerHTML = '<div class="error">图片读取失败。</div>';
    };
    reader.readAsDataURL(file);
  }

  async function sendDirect(currentDraft, caption) {
    if (!currentDraft) return;
    try {
      await CM.sendDirectPayload({message:caption, image:{filename:currentDraft.filename, data_url:currentDraft.data_url}});
    } catch (error) {
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `发送图片失败：${error.message}`;
      CM.dom.chat.appendChild(box);
    }
  }

  async function sendCurrentDraft() {
    const currentDraft = draft;
    if (!currentDraft) return;
    const caption = document.getElementById("imageCaption")?.value.trim() || "";
    close();
    if (CM.isGroupConversation()) return CM.features.groups?.sendImage?.(currentDraft, caption);
    return sendDirect(currentDraft, caption);
  }

  trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "image-trigger";
  trigger.textContent = "▧";
  trigger.title = "发送图片";
  CM.dom.composer.insertBefore(trigger, CM.dom.input);

  inputEl = document.createElement("input");
  inputEl.type = "file";
  inputEl.accept = "image/jpeg,image/png,image/gif,image/webp";
  inputEl.className = "image-file-input";
  document.body.appendChild(inputEl);

  panel = document.createElement("div");
  panel.className = "image-panel hidden";
  document.querySelector(".composer-wrap")?.appendChild(panel);

  lightbox = document.createElement("div");
  lightbox.className = "image-lightbox hidden";
  lightbox.setAttribute("role", "dialog");
  lightbox.setAttribute("aria-modal", "true");
  lightbox.setAttribute("aria-label", "图片预览");
  lightbox.innerHTML = '<button type="button" class="image-lightbox-close" data-image-lightbox-close aria-label="关闭图片预览">×</button><div class="image-lightbox-stage"><img alt=""></div><div class="image-lightbox-actions"><button type="button" data-image-set-avatar hidden>设为当前头像</button></div>';
  document.body.appendChild(lightbox);

  trigger.addEventListener("click", event => {
    event.stopPropagation();
    CM.features.stickers?.close?.();
    CM.features.aiImages?.close?.();
    inputEl.click();
  });
  inputEl.addEventListener("change", () => openDraft(inputEl.files?.[0], {source:"FILE_PICKER"}));
  CM.dom.input.addEventListener("paste", event => {
    const items = [...(event.clipboardData?.items || [])];
    const imageItem = items.find(item => item.kind === "file" && String(item.type || "").startsWith("image/"));
    if (!imageItem) return;
    const file = imageItem.getAsFile();
    if (!file) return;
    event.preventDefault();
    openDraft(file, {source:"CLIPBOARD"});
  });
  panel.addEventListener("click", event => {
    event.stopPropagation();
    if (event.target.closest("[data-image-cancel]")) close();
    if (event.target.closest("[data-image-send]")) sendCurrentDraft().catch(console.error);
  });
  panel.addEventListener("keydown", event => {
    if (!event.target.closest("#imageCaption")) return;
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    sendCurrentDraft().catch(console.error);
  });
  CM.dom.chat.addEventListener("click", event => {
    const image = event.target.closest(".image-bubble img");
    if (!image) return;
    event.preventDefault();
    openLightbox(image);
  });
  lightbox.addEventListener("click", event => {
    if (event.target === lightbox || event.target.closest("[data-image-lightbox-close]")) { closeLightbox(); return; }
    const avatarButton = event.target.closest("[data-image-set-avatar]");
    if (avatarButton && lightboxAvatarSource) {
      avatarButton.disabled = true;
      avatarButton.textContent = "正在设置…";
      CM.features.avatars?.useChatImage?.(lightboxAvatarSource)
        .then(() => closeLightbox())
        .catch(error => {
          avatarButton.disabled = false;
          avatarButton.textContent = `设置失败：${error.message}`;
        });
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !lightbox.classList.contains("hidden")) closeLightbox();
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".image-panel") && !event.target.closest(".image-trigger") && !panel?.contains(document.activeElement)) close();
  });
  CM.on("conversationChanged", () => { close(); closeLightbox(); });

  CM.registerFeature("images", {openDraft, openDataDraft, close, setDisabled, openLightbox, closeLightbox, trigger});
})();
