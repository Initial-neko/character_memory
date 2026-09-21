(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before avatars.js");

  let managerCharacterId = null;
  let currentSearch = null;
  let visualProviders = null;
  let applying = false;

  const profileById = id => CM.state.characters.find(item => item.id === id) || null;
  const profileByName = name => CM.state.characters.find(item => (item.name || item.id) === name) || null;

  function imageHtml(profile, className = "") {
    if (!profile?.avatar_url) return "";
    return `<img class="character-avatar-image ${CM.escapeHtml(className)}" src="${CM.escapeHtml(profile.avatar_url)}" alt="${CM.escapeHtml(profile.name || profile.id || "头像")}" loading="lazy">`;
  }

  function setAvatar(container, profile) {
    if (!container || !profile) return;
    const url = profile.avatar_url || "";
    const marker = `${profile.id}|${url}`;
    if (url && container.dataset.avatarBroken === url) {
      container.dataset.avatarMarker = marker;
      if (container.querySelector("img")) container.textContent = CM.initialFor(profile);
      return;
    }
    const existingImage = container.querySelector("img.character-avatar-image");
    const imageMatches = url ? existingImage?.getAttribute("src") === url : !existingImage;
    if (container.dataset.avatarMarker === marker && imageMatches) return;
    container.dataset.avatarMarker = marker;
    if (container.dataset.avatarBroken && container.dataset.avatarBroken !== url) delete container.dataset.avatarBroken;
    container.innerHTML = imageHtml(profile) || CM.escapeHtml(CM.initialFor(profile));
  }

  function applyAvatars() {
    if (applying) return;
    applying = true;
    try {
      CM.dom.characterList.querySelectorAll("[data-character]").forEach(button => {
        const profile = profileById(button.dataset.character);
        setAvatar(button.querySelector(".character-avatar"), profile);
      });
      if (!CM.isGroupConversation()) {
        const current = CM.currentProfile();
        setAvatar(CM.dom.headerAvatar, current);
        CM.dom.chat.querySelectorAll(".message-row.assistant:not(.group-assistant) .avatar").forEach(node => setAvatar(node, current));
      }
      CM.dom.chat.querySelectorAll(".message-row.group-assistant").forEach(row => {
        const name = row.querySelector(".group-speaker-name")?.textContent?.trim() || "";
        const profile = profileByName(name);
        if (profile) setAvatar(row.querySelector(".avatar"), profile);
      });
      document.querySelectorAll(".character-avatar-image:not([data-avatar-wired])").forEach(img => {
        img.dataset.avatarWired = "1";
        img.addEventListener("error", () => {
          const host = img.parentElement;
          const id = host?.closest("[data-character]")?.dataset.character || (CM.isGroupConversation() ? "" : CM.state.characterId);
          const profile = profileById(id);
          if (host && profile) {
            host.dataset.avatarMarker = `${profile.id}|${profile.avatar_url || ""}`;
            host.dataset.avatarBroken = profile.avatar_url || "broken";
            host.textContent = CM.initialFor(profile);
          }
        }, {once:true});
      });
    } finally {
      applying = false;
    }
  }

  async function refreshProfiles() {
    const data = await CM.api("/v1/character-profiles");
    const incoming = data.characters || [];
    if (!incoming.length) return;
    CM.state.characters = incoming;
    CM.renderCharacterList();
    CM.updateHeader();
    applyAvatars();
  }

  function providerOptions() {
    const list = visualProviders?.providers || [];
    if (!list.length) return '<option value="agnes">Agnes</option><option value="msimg">msimg</option>';
    return list.map(item => {
      const label = `${item.id === "agnes" ? "Agnes 2.5 Flash" : "msimg / ModelScope"}${item.configured ? "" : "（未配置）"}`;
      return `<option value="${CM.escapeHtml(item.id)}" ${item.configured ? "" : "disabled"}>${CM.escapeHtml(label)}</option>`;
    }).join("");
  }

  function managerHtml(profile) {
    const avatar = imageHtml(profile, "avatar-manager-current-image");
    const configured = (visualProviders?.providers || []).some(item => item.configured);
    return `<div class="avatar-manager">
      <section class="avatar-manager-current">
        <div class="avatar-manager-current-preview">${avatar || `<span>${CM.escapeHtml(CM.initialFor(profile))}</span>`}</div>
        <div><strong>${CM.escapeHtml(profile.name || profile.id)}</strong><p>${profile.avatar_url ? "当前头像已保存到本地。之后更好的聊天图片/自拍也可以替换它。" : "当前还没有头像，将继续使用首字母。"}</p></div>
      </section>
      <div class="avatar-planning-note"><strong>同一个视觉身份，两种取图方式</strong><span>搜索会把 LLM 规划后的短关键词发给搜索引擎；AI 生成会让 LLM 先编译角色视觉提示词，再交给所选图像 Provider。普通聊天不会暴露裸 ImageGen 控制台。</span></div>
      <label class="ui-field avatar-search-field"><span>补充偏好（可选）</span><input type="text" data-avatar-hint maxlength="600" value="" placeholder="例如：更温暖、自然近景、保持经典造型"></label>
      <div class="ui-actions"><button class="primary" type="button" data-avatar-search>让角色决定并搜索</button></div>
      <div class="ui-field-row avatar-generation-controls">
        <label class="ui-field avatar-provider-field"><span>AI 生成来源</span><select data-avatar-provider>${providerOptions()}</select></label>
        <button type="button" data-avatar-generate ${configured ? "" : "disabled"}>生成一个候选头像</button>
      </div>
      <div data-avatar-results class="avatar-results"><p class="ui-hint">生成结果先作为候选保存，不会自动替换当前头像。</p></div>
    </div>`;
  }

  async function open(characterId = CM.state.characterId) {
    const profile = profileById(characterId);
    if (!profile) return;
    managerCharacterId = characterId;
    currentSearch = null;
    try { visualProviders = await CM.api("/v1/visual/providers"); } catch (error) { console.warn("visual providers unavailable", error); visualProviders = null; }
    CM.openDrawer(`${profile.name || profile.id} · 头像`, "搜索现成图片、AI 生成候选，或把聊天里的图片设为头像");
    CM.dom.drawerBody.innerHTML = managerHtml(profile);
    const preferred = visualProviders?.default;
    const select = CM.dom.drawerBody.querySelector("[data-avatar-provider]");
    if (preferred && select?.querySelector(`option[value="${CSS.escape(preferred)}"]:not(:disabled)`)) select.value = preferred;
    CM.dom.drawerBody.querySelector("[data-avatar-hint]")?.focus();
  }

  function renderCandidates(result) {
    const box = CM.dom.drawerBody.querySelector("[data-avatar-results]");
    if (!box) return;
    const candidates = result?.candidates || [];
    const plannedQueries = result?.queries || (result?.query ? [result.query] : []);
    const usedQueries = result?.used_queries || [];
    const sourceNote = result?.planning_source === "fallback"
      ? '<span class="avatar-plan-fallback">本次 LLM 规划不可用，已使用基础关键词兜底。</span>' : "";
    const plan = `<div class="avatar-search-plan"><strong>此刻的头像判断</strong><p>${CM.escapeHtml(result?.visual_intent || "保持人物辨识度的清晰聊天头像")}</p>${result?.preferred_mood ? `<span>气质：${CM.escapeHtml(result.preferred_mood)}</span>` : ""}${result?.preferred_style ? `<span>风格：${CM.escapeHtml(result.preferred_style)}</span>` : ""}${plannedQueries.length ? `<details><summary>AI 搜索词</summary><div>${plannedQueries.map(item => `<code>${CM.escapeHtml(item)}</code>`).join("")}</div></details>` : ""}${usedQueries.length > 1 ? `<span>实际使用了 ${usedQueries.length} 条搜索词。</span>` : ""}${sourceNote}</div>`;
    if (!candidates.length) { box.innerHTML = `${plan}<div class="error">没有找到适合作为头像的图片。</div>`; return; }
    box.innerHTML = `${plan}<div class="avatar-search-summary">找到 ${candidates.length} 个候选</div><div class="avatar-candidate-grid">${candidates.map(item => {
      const size = item.width && item.height ? `${item.width}×${item.height}` : "尺寸未知";
      return `<article class="avatar-candidate"><div class="avatar-candidate-image"><img src="${CM.escapeHtml(item.thumbnail_url)}" alt="${CM.escapeHtml(item.title || "头像候选")}" loading="lazy"></div><div class="avatar-candidate-copy"><strong>${CM.escapeHtml(item.title || "头像候选")}</strong><span>${CM.escapeHtml(item.source_domain || "未知来源")} · ${CM.escapeHtml(size)}</span></div><div class="avatar-candidate-actions"><a href="${CM.escapeHtml(item.source_page_url)}" target="_blank" rel="noopener noreferrer">来源</a><button type="button" data-avatar-select="${CM.escapeHtml(item.id)}">选这个</button></div></article>`;
    }).join("")}</div>`;
  }

  async function search() {
    if (!managerCharacterId) return;
    const hint = CM.dom.drawerBody.querySelector("[data-avatar-hint]")?.value.trim() || "";
    const button = CM.dom.drawerBody.querySelector("[data-avatar-search]");
    const box = CM.dom.drawerBody.querySelector("[data-avatar-results]");
    if (button) { button.disabled = true; button.textContent = "正在规划并搜索…"; }
    if (box) box.innerHTML = '<p class="muted">角色状态正在被压缩成安全的图片搜索词…</p>';
    try {
      currentSearch = await CM.api(`/v1/characters/${encodeURIComponent(managerCharacterId)}/avatar/search`, {method:"POST", body:JSON.stringify({hint, limit:12})});
      renderCandidates(currentSearch);
    } catch (error) {
      if (box) box.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    } finally {
      if (button) { button.disabled = false; button.textContent = "让角色决定并搜索"; }
    }
  }

  async function generate() {
    if (!managerCharacterId) return;
    const provider = CM.dom.drawerBody.querySelector("[data-avatar-provider]")?.value || "";
    const hint = CM.dom.drawerBody.querySelector("[data-avatar-hint]")?.value.trim() || "";
    const button = CM.dom.drawerBody.querySelector("[data-avatar-generate]");
    const box = CM.dom.drawerBody.querySelector("[data-avatar-results]");
    if (button) { button.disabled = true; button.textContent = "正在生成…"; }
    if (box) box.innerHTML = `<p class="muted">正在通过 ${CM.escapeHtml(provider)} 生成候选头像。当前头像在 Provider 支持时会作为 identity reference。</p>`;
    try {
      const result = await CM.api(`/v1/characters/${encodeURIComponent(managerCharacterId)}/avatar/generate`, {method:"POST", body:JSON.stringify({provider, hint})});
      const candidate = result.candidate;
      box.innerHTML = `<div class="avatar-search-plan"><strong>AI 生成候选</strong><p>${CM.escapeHtml(result.visual_intent || "保持人物身份一致")}</p><span>${CM.escapeHtml(candidate.provider)} · ${CM.escapeHtml(candidate.model || "默认模型")} · ${CM.fmtMs(result.duration_ms)}</span>${candidate.supports_reference_images ? '<span>已支持当前头像作为参考身份锚点。</span>' : '<span>当前 Provider 为纯文生图路径，依赖 Prompt 保持身份。</span>'}</div><article class="avatar-generated-candidate"><img src="${CM.escapeHtml(candidate.url)}" alt="AI 生成头像候选"><div class="avatar-candidate-actions"><button class="primary" type="button" data-avatar-use-media="${CM.escapeHtml(candidate.media_id)}">设为头像</button><button type="button" data-avatar-generate-again>再生成一张</button></div></article>`;
    } catch (error) {
      if (box) box.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    } finally {
      if (button) { button.disabled = false; button.textContent = "生成一个候选头像"; }
    }
  }

  async function showSelected() {
    await refreshProfiles();
    const profile = profileById(managerCharacterId || CM.state.characterId);
    if (CM.dom.drawer.classList.contains("open") && managerCharacterId) {
      CM.dom.drawerBody.innerHTML = `<div class="avatar-selected-success"><div class="avatar-manager-current-preview large">${imageHtml(profile, "avatar-manager-current-image") || CM.escapeHtml(CM.initialFor(profile))}</div><h3>头像已更新</h3><p>图片已复制到本地 Avatar 目录；原聊天图片或候选之后即使变化，也不会让头像失效。</p><button type="button" data-avatar-search-again>继续选择</button></div>`;
    }
    applyAvatars();
  }

  async function useChatImage({mediaId = "", imageId = "", characterId = CM.state.characterId} = {}) {
    if (!characterId || CM.isGroupConversation()) throw new Error("当前只支持单聊图片设为头像");
    const body = mediaId ? {media_id:mediaId} : {image_id:imageId};
    if (!mediaId && !imageId) throw new Error("无法识别这张聊天图片的资源 ID");
    await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/avatar/from-chat`, {method:"POST", body:JSON.stringify(body)});
    managerCharacterId = characterId;
    await showSelected();
  }

  async function select(candidateId) {
    if (!managerCharacterId || !currentSearch?.search_id || !candidateId) return;
    const buttons = [...CM.dom.drawerBody.querySelectorAll("[data-avatar-select]")];
    buttons.forEach(button => { button.disabled = true; });
    const selected = buttons.find(button => button.dataset.avatarSelect === candidateId);
    if (selected) selected.textContent = "正在保存…";
    try {
      await CM.api(`/v1/characters/${encodeURIComponent(managerCharacterId)}/avatar/select`, {method:"POST", body:JSON.stringify({search_id:currentSearch.search_id, candidate_id:candidateId})});
      await showSelected();
    } catch (error) {
      const box = CM.dom.drawerBody.querySelector("[data-avatar-results]");
      if (box) box.insertAdjacentHTML("afterbegin", `<div class="error">${CM.escapeHtml(error.message)}</div>`);
      buttons.forEach(button => { button.disabled = false; });
      if (selected) selected.textContent = "选这个";
    }
  }

  CM.dom.headerAvatar.classList.add("avatar-editable");
  CM.dom.headerAvatar.title = "设置头像";
  CM.dom.headerAvatar.addEventListener("click", () => { if (!CM.isGroupConversation()) open().catch(console.error); });

  CM.dom.drawerBody.addEventListener("click", event => {
    if (event.target.closest("[data-avatar-search]")) { search().catch(console.error); return; }
    if (event.target.closest("[data-avatar-generate]")) { generate().catch(console.error); return; }
    if (event.target.closest("[data-avatar-generate-again]")) { generate().catch(console.error); return; }
    if (event.target.closest("[data-avatar-search-again]")) { open(managerCharacterId).catch(console.error); return; }
    const generated = event.target.closest("[data-avatar-use-media]");
    if (generated) { useChatImage({mediaId:generated.dataset.avatarUseMedia, characterId:managerCharacterId}).catch(console.error); return; }
    const button = event.target.closest("[data-avatar-select]");
    if (button) select(button.dataset.avatarSelect).catch(console.error);
  });
  CM.dom.drawerBody.addEventListener("keydown", event => {
    if (!event.target.closest("[data-avatar-hint]") || event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    search().catch(console.error);
  });

  const observer = new MutationObserver(() => applyAvatars());
  observer.observe(CM.dom.chat, {childList:true, subtree:true});
  observer.observe(CM.dom.characterList, {childList:true, subtree:true});
  observer.observe(CM.dom.headerAvatar, {childList:true, subtree:true});

  CM.on("charactersLoaded", () => refreshProfiles().catch(error => console.warn("avatar profile refresh failed", error)));
  CM.on("historyLoaded", applyAvatars);
  CM.on("conversationChanged", applyAvatars);
  CM.registerFeature("avatars", {open, refreshProfiles, apply:applyAvatars, useChatImage});
})();
