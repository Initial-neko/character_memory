(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before stickers.js");

  let cache = null;
  let selectedPackId = null;
  let panel = null;
  let importInput = null;
  let importFile = null;
  let importNotice = "";
  let trigger = null;

  const stickerAsset = id => `/v1/stickers/${encodeURIComponent(id)}/asset`;

  async function load({refresh = false} = {}) {
    if (refresh) cache = null;
    if (cache) return cache;
    const data = await CM.api("/v1/stickers");
    cache = (data.stickers || []).map(item => ({...item, pack_id:item.pack_id || "default", pack_name:item.pack_name || "内置", url:item.url || stickerAsset(item.id)}));
    return cache;
  }

  function close() { panel?.classList.add("hidden"); }
  function setDisabled(disabled) { if (trigger) trigger.disabled = Boolean(disabled); }

  function packs(stickers) {
    const result = new Map();
    for (const item of stickers) {
      const id = item.pack_id || "default";
      if (!result.has(id)) result.set(id, {id, name:item.pack_name || "表情包", stickers:[]});
      result.get(id).stickers.push(item);
    }
    return [...result.values()];
  }

  const toolbar = () => `<div class="sticker-panel-toolbar"><strong>表情包</strong><button type="button" class="sticker-import-open" data-sticker-import-open title="导入全局表情包">＋</button></div>`;

  function wireFallbacks() {
    panel?.querySelectorAll(".sticker-choice img").forEach(img => img.addEventListener("error", () => img.closest(".sticker-choice")?.classList.add("broken"), {once:true}));
  }

  function render(stickers) {
    if (!panel) return;
    if (!stickers.length) { panel.innerHTML = `${toolbar()}<div class="sticker-loading">还没有可用表情包。</div>`; return; }
    const allPacks = packs(stickers);
    if (!allPacks.some(pack => pack.id === selectedPackId)) selectedPackId = allPacks[0].id;
    const active = allPacks.find(pack => pack.id === selectedPackId) || allPacks[0];
    const tabs = allPacks.length > 1 ? `<div class="sticker-pack-tabs">${allPacks.map(pack => `<button type="button" class="sticker-pack-tab${pack.id === active.id ? " active" : ""}" data-sticker-pack="${CM.escapeHtml(pack.id)}" title="${CM.escapeHtml(pack.name)}">${CM.escapeHtml(pack.name)}</button>`).join("")}</div>` : "";
    const grid = `<div class="sticker-grid">${active.stickers.map(item => `<button type="button" class="sticker-choice" data-sticker-id="${CM.escapeHtml(item.id)}" title="${CM.escapeHtml(item.label)}"><img src="${CM.escapeHtml(item.url || stickerAsset(item.id))}" alt="${CM.escapeHtml(item.label)}"><span class="sticker-choice-fallback">表情</span></button>`).join("")}</div>`;
    const notice = importNotice ? `<div class="sticker-import-notice">${CM.escapeHtml(importNotice)}</div>` : "";
    panel.innerHTML = `${toolbar()}${notice}${tabs}${grid}<div class="sticker-pack-foot">${CM.escapeHtml(active.name)} · ${active.stickers.length} 张 · 全局</div>`;
    wireFallbacks();
  }

  async function open({refresh = false} = {}) {
    if (!panel) return;
    panel.classList.remove("hidden");
    panel.innerHTML = '<div class="sticker-loading">正在拿表情包…</div>';
    try { render(await load({refresh})); }
    catch (error) { panel.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`; }
  }

  function showImportDialog(file) {
    importFile = file;
    panel.classList.remove("hidden");
    panel.innerHTML = `<div class="sticker-import-card"><div class="sticker-import-title">导入全局表情包</div><div class="sticker-import-file">${CM.escapeHtml(file.name)} · ${(file.size / 1024 / 1024).toFixed(1)} MiB</div><div class="sticker-import-help">导入后所有人物和群聊都能使用。推荐 ZIP 内带 <code>all_tags.json</code> 或每组 <code>tags.json</code>；只有图片时可让 Vision 自动补标签。</div><label class="sticker-auto-tag"><input type="checkbox" data-sticker-auto-tag checked> <span>AI 自动补标签 <small>只补缺失标签，不覆盖已有标注</small></span></label><div class="sticker-import-actions"><button type="button" data-sticker-import-cancel>取消</button><button type="button" class="sticker-import-confirm" data-sticker-import-confirm>导入</button></div></div>`;
  }

  function importError(payload, status) {
    const detail = payload?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(item => item?.msg || JSON.stringify(item)).join("；");
    return `导入失败（HTTP ${status}）`;
  }

  async function importStickerFile() {
    const file = importFile;
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".zip")) { panel.innerHTML = '<div class="error">Web 导入使用 ZIP 格式。</div>'; return; }
    if (file.size > 64 * 1024 * 1024) { panel.innerHTML = '<div class="error">表情包 ZIP 不能超过 64 MiB。</div>'; return; }
    const autoTag = Boolean(panel.querySelector("[data-sticker-auto-tag]")?.checked);
    panel.innerHTML = `<div class="sticker-loading"><strong>正在导入 ${CM.escapeHtml(file.name)}</strong><br><span>${autoTag ? "缺标签的图片会调用 Vision。" : "只读取 ZIP 内现有标签。"}</span></div>`;
    const params = new URLSearchParams({filename:file.name, auto_tag:autoTag ? "true" : "false"});
    try {
      const response = await fetch(`/v1/stickers/import?${params.toString()}`, {method:"POST", headers:{"Content-Type":"application/zip"}, body:file});
      let payload = {};
      try { payload = await response.json(); } catch (_) { payload = {}; }
      if (!response.ok) throw new Error(importError(payload, response.status));
      cache = null;
      if (payload.packs?.[0]?.id) selectedPackId = payload.packs[0].id;
      importNotice = `已全局导入 ${payload.imported || 0} 张${payload.ai_tagged ? ` · AI 标注 ${payload.ai_tagged} 张` : ""}`;
      importFile = null;
      await open({refresh:true});
    } catch (error) {
      panel.innerHTML = `${toolbar()}<div class="error">${CM.escapeHtml(error.message)}</div><div class="sticker-import-retry"><button type="button" data-sticker-import-open>重新选择 ZIP</button></div>`;
    }
  }

  async function sendDirect(sticker) {
    if (!sticker) return;
    close();
    try { await CM.sendDirectPayload({message:"", sticker_id:sticker.id}); }
    catch (error) {
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `发送表情包失败：${error.message}`;
      CM.dom.chat.appendChild(box);
    }
  }

  async function send(sticker) {
    if (CM.isGroupConversation()) return CM.features.groups?.sendSticker?.(sticker);
    return sendDirect(sticker);
  }

  trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "sticker-trigger";
  trigger.textContent = "☺";
  trigger.title = "发送表情包";
  CM.dom.composer.insertBefore(trigger, CM.dom.input);
  panel = document.createElement("div");
  panel.className = "sticker-panel hidden";
  document.querySelector(".composer-wrap")?.appendChild(panel);
  importInput = document.createElement("input");
  importInput.type = "file";
  importInput.accept = ".zip,application/zip";
  importInput.className = "sticker-import-input";
  document.body.appendChild(importInput);

  trigger.addEventListener("click", event => { event.stopPropagation(); if (panel.classList.contains("hidden")) open(); else close(); });
  importInput.addEventListener("change", () => { const file = importInput.files?.[0]; if (file) showImportDialog(file); importInput.value = ""; });
  panel.addEventListener("click", async event => {
    event.stopPropagation();
    if (event.target.closest("[data-sticker-import-open]")) { importFile = null; importInput.click(); return; }
    if (event.target.closest("[data-sticker-import-cancel]")) { importFile = null; await open(); return; }
    if (event.target.closest("[data-sticker-import-confirm]")) { await importStickerFile(); return; }
    const packButton = event.target.closest("[data-sticker-pack]");
    if (packButton) { selectedPackId = packButton.dataset.stickerPack; render(await load()); return; }
    const button = event.target.closest("[data-sticker-id]");
    if (!button) return;
    const sticker = (await load()).find(item => item.id === button.dataset.stickerId);
    await send(sticker);
  });
  document.addEventListener("click", event => { if (!event.target.closest(".sticker-panel") && !event.target.closest(".sticker-trigger")) close(); });
  CM.on("conversationChanged", () => close());

  CM.registerFeature("stickers", {load, open, close, send, setDisabled, trigger});
})();
