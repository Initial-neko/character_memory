(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before ai_images.js");

  let trigger = null;
  let panel = null;
  let lastPrompt = "";
  let busy = false;

  function close() {
    panel?.classList.add("hidden");
  }

  function syncState() {
    if (!trigger) return;
    const disabled = CM.isGroupConversation();
    trigger.disabled = disabled;
    trigger.title = disabled ? "AI 生成图片暂只支持单聊" : "AI 生成图片";
    if (disabled) close();
  }

  function requestBody() {
    const instruction = panel?.querySelector("[data-ai-image-instruction]")?.value.trim() || "";
    const purpose = panel?.querySelector("[data-ai-image-purpose]")?.value || "SCENE";
    const useAvatar = Boolean(panel?.querySelector("[data-ai-image-reference]")?.checked);
    return {instruction, purpose, use_avatar_reference:useAvatar};
  }

  function setStatus(text, kind = "") {
    const status = panel?.querySelector("[data-ai-image-status]");
    if (!status) return;
    status.textContent = text;
    status.className = `ai-image-status ${kind}`.trim();
  }

  function setPrompt(prompt) {
    lastPrompt = String(prompt || "").trim();
    const box = panel?.querySelector("[data-ai-image-prompt]");
    const copy = panel?.querySelector("[data-ai-image-copy]");
    if (box) {
      box.textContent = lastPrompt;
      box.hidden = !lastPrompt;
    }
    if (copy) copy.hidden = !lastPrompt;
  }

  function setBusy(value) {
    busy = Boolean(value);
    panel?.querySelectorAll("button, select, textarea, input").forEach(element => {
      if (element.matches("[data-ai-image-close]")) return;
      element.disabled = busy;
    });
  }

  function open() {
    if (CM.isGroupConversation()) return;
    CM.features.stickers?.close?.();
    CM.features.images?.close?.();
    panel.classList.remove("hidden");
    panel.querySelector("[data-ai-image-instruction]")?.focus();
  }

  async function rewrite() {
    const body = requestBody();
    if (!body.instruction) {
      setStatus("先写一句你想生成什么。", "error");
      return;
    }
    setBusy(true);
    setStatus("AI 正在把你的描述润色成绘图 Prompt…");
    try {
      const characterId = encodeURIComponent(CM.state.characterId);
      const data = await CM.api(`/v1/characters/${characterId}/images/rewrite`, {
        method:"POST",
        body:JSON.stringify(body),
      });
      setPrompt(data.prompt);
      setStatus(`润色完成 · ${data.duration_ms ?? "-"} ms · ${data.aspect_ratio || "-"}`);
    } catch (error) {
      setStatus(`润色失败：${error.message}`, "error");
    } finally {
      setBusy(false);
    }
  }

  async function generate() {
    const body = requestBody();
    if (!body.instruction) {
      setStatus("先写一句你想生成什么。", "error");
      return;
    }
    setBusy(true);
    setStatus("AI 正在润色并生成图片，这可能需要一会儿…");
    try {
      const characterId = encodeURIComponent(CM.state.characterId);
      const data = await CM.api(`/v1/characters/${characterId}/images/generate`, {
        method:"POST",
        body:JSON.stringify(body),
      });
      setPrompt(data.prompt);
      const image = data.image || {};
      if (!image.data_url) throw new Error("生成接口没有返回可发送的图片草稿");
      setStatus(`生成完成 · ${data.duration_ms ?? "-"} ms · ${data.provider || "-"} / ${data.model || "-"}`);
      close();
      CM.features.images?.openDataDraft?.({
        filename:image.filename || `ai-generated-${String(body.purpose || "scene").toLowerCase()}.png`,
        data_url:image.data_url,
        size:image.size_bytes || 0,
        source:"AI_GENERATED",
      });
    } catch (error) {
      setStatus(`生成失败：${error.message}`, "error");
    } finally {
      setBusy(false);
    }
  }

  async function copyPrompt() {
    if (!lastPrompt) return;
    const button = panel?.querySelector("[data-ai-image-copy]");
    const original = button?.textContent || "复制 Prompt";
    try {
      await navigator.clipboard.writeText(lastPrompt);
      if (button) button.textContent = "已复制";
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = lastPrompt;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
      if (button) button.textContent = "已复制";
    } finally {
      setTimeout(() => { if (button) button.textContent = original; }, 1200);
    }
  }

  trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "ai-image-trigger";
  trigger.textContent = "✦";
  trigger.title = "AI 生成图片";
  trigger.setAttribute("aria-label", "AI 生成图片");
  CM.dom.composer.insertBefore(trigger, CM.dom.input);

  panel = document.createElement("div");
  panel.className = "ai-image-panel hidden";
  panel.innerHTML = `
    <div class="ai-image-head"><strong>AI 生成图片</strong><button type="button" data-ai-image-close aria-label="关闭">×</button></div>
    <div class="ai-image-options">
      <label>用途<select data-ai-image-purpose><option value="SCENE">场景 / 配图</option><option value="SELFIE">角色自拍</option></select></label>
      <label class="ai-image-check"><input type="checkbox" data-ai-image-reference> 使用当前头像作为人物身份参考</label>
    </div>
    <textarea data-ai-image-instruction rows="4" maxlength="1600" placeholder="例如：画一张 Rin 在图书馆窗边看雨的日常场景，安静一点，不要像棚拍。"></textarea>
    <div class="ai-image-actions">
      <button type="button" class="secondary" data-ai-image-rewrite>润色 Prompt</button>
      <button type="button" data-ai-image-generate>生成图片</button>
      <button type="button" class="secondary" data-ai-image-copy hidden>复制 Prompt</button>
    </div>
    <div class="ai-image-status" data-ai-image-status>生成后会进入图片草稿，你确认后再发送。</div>
    <pre class="ai-image-prompt" data-ai-image-prompt hidden></pre>`;
  document.querySelector(".composer-wrap")?.appendChild(panel);

  trigger.addEventListener("click", event => {
    event.stopPropagation();
    if (panel.classList.contains("hidden")) open(); else close();
  });
  panel.addEventListener("click", event => {
    event.stopPropagation();
    if (event.target.closest("[data-ai-image-close]")) { close(); return; }
    if (event.target.closest("[data-ai-image-rewrite]")) { rewrite().catch(console.error); return; }
    if (event.target.closest("[data-ai-image-generate]")) { generate().catch(console.error); return; }
    if (event.target.closest("[data-ai-image-copy]")) copyPrompt().catch(console.error);
  });
  panel.querySelector("[data-ai-image-purpose]")?.addEventListener("change", event => {
    const checkbox = panel.querySelector("[data-ai-image-reference]");
    if (!checkbox) return;
    checkbox.checked = event.target.value === "SELFIE";
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".ai-image-panel") && !event.target.closest(".ai-image-trigger") && !panel?.contains(document.activeElement)) close();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !panel.classList.contains("hidden") && !busy) close();
  });
  CM.on("conversationChanged", syncState);
  CM.on("ready", syncState);

  syncState();
  CM.registerFeature("aiImages", {open, close, rewrite, generate, syncState, trigger});
})();
