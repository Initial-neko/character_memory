(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before avatars.js");

  let managerCharacterId = null;
  let currentSearch = null;
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

  function managerHtml(profile) {
    const avatar = imageHtml(profile, "avatar-manager-current-image");
    return `<div class="avatar-manager">
      <section class="avatar-manager-current">
        <div class="avatar-manager-current-preview">${avatar || `<span>${CM.escapeHtml(CM.initialFor(profile))}</span>`}</div>
        <div><strong>${CM.escapeHtml(profile.name || profile.id)}</strong><p>${profile.avatar_url ? "当前头像已保存到本地。" : "当前还没有头像，将继续使用首字母。"}</p></div>
      </section>
      <div class="avatar-planning-note"><strong>让角色自己判断</strong><span>系统会结合人物设定、当前状态和最近互动，先判断此刻适合什么头像，再生成图片搜索词。</span></div>
      <label class="avatar-search-field"><span>补充偏好（可选）</span><input type="text" data-avatar-hint maxlength="400" value="" placeholder="例如：更温暖一点、害羞一点，或保持经典形象"></label>
      <div class="avatar-manager-actions"><button class="primary" type="button" data-avatar-search>让角色决定并搜索</button></div>
      <div data-avatar-results class="avatar-results"><p class="muted">SearchAPI 只会收到 AI 生成的短搜索词，不会收到 Persona、Mental State 或聊天原文。</p></div>
    </div>`;
  }

  async function open(characterId = CM.state.characterId) {
    const profile = profileById(characterId);
    if (!profile) return;
    managerCharacterId = characterId;
    currentSearch = null;
    CM.openDrawer(`${profile.name || profile.id} · 头像`, "先由角色判断此刻需要什么头像，再搜索候选");
    CM.dom.drawerBody.innerHTML = managerHtml(profile);
    CM.dom.drawerBody.querySelector("[data-avatar-hint]")?.focus();
  }

  function renderCandidates(result) {
    const box = CM.dom.drawerBody.querySelector("[data-avatar-results]");
    if (!box) return;
    const candidates = result?.candidates || [];
    const plannedQueries = result?.queries || (result?.query ? [result.query] : []);
    const usedQueries = result?.used_queries || [];
    const sourceNote = result?.planning_source === "fallback"
      ? '<span class="avatar-plan-fallback">本次 LLM 规划不可用，已使用基础关键词兜底。</span>'
      : "";
    const plan = `<div class="avatar-search-plan">
      <strong>此刻的头像判断</strong>
      <p>${CM.escapeHtml(result?.visual_intent || "保持人物辨识度的清晰聊天头像")}</p>
      ${result?.preferred_mood ? `<span>气质：${CM.escapeHtml(result.preferred_mood)}</span>` : ""}
      ${result?.preferred_style ? `<span>风格：${CM.escapeHtml(result.preferred_style)}</span>` : ""}
      ${plannedQueries.length ? `<details><summary>AI 搜索词</summary><div>${plannedQueries.map(item => `<code>${CM.escapeHtml(item)}</code>`).join("")}</div></details>` : ""}
      ${usedQueries.length > 1 ? `<span>首条候选不足，实际使用了 ${usedQueries.length} 条搜索词。</span>` : ""}
      ${sourceNote}
    </div>`;
    if (!candidates.length) {
      box.innerHTML = `${plan}<div class="error">没有找到适合作为头像的图片，可以补充偏好再试。</div>`;
      return;
    }
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
    if (button) { button.disabled = true; button.textContent = "正在判断并搜索…"; }
    if (box) box.innerHTML = '<p class="muted">角色正在结合当前状态判断适合的头像，并生成搜索词…</p>';
    try {
      currentSearch = await CM.api(`/v1/characters/${encodeURIComponent(managerCharacterId)}/avatar/search`, {method:"POST", body:JSON.stringify({hint, limit:12})});
      renderCandidates(currentSearch);
    } catch (error) {
      if (box) box.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    } finally {
      if (button) { button.disabled = false; button.textContent = "让角色决定并搜索"; }
    }
  }

  async function select(candidateId) {
    if (!managerCharacterId || !currentSearch?.search_id || !candidateId) return;
    const buttons = [...CM.dom.drawerBody.querySelectorAll("[data-avatar-select]")];
    buttons.forEach(button => { button.disabled = true; });
    const selected = buttons.find(button => button.dataset.avatarSelect === candidateId);
    if (selected) selected.textContent = "正在保存…";
    try {
      await CM.api(`/v1/characters/${encodeURIComponent(managerCharacterId)}/avatar/select`, {
        method:"POST",
        body:JSON.stringify({search_id:currentSearch.search_id, candidate_id:candidateId}),
      });
      await refreshProfiles();
      const profile = profileById(managerCharacterId);
      CM.dom.drawerBody.innerHTML = `<div class="avatar-selected-success"><div class="avatar-manager-current-preview large">${imageHtml(profile, "avatar-manager-current-image") || CM.escapeHtml(CM.initialFor(profile))}</div><h3>头像已更新</h3><p>图片已经下载到本地 Avatar 目录，聊天界面不会依赖原图热链。</p><button type="button" data-avatar-search-again>重新选择</button></div>`;
      applyAvatars();
    } catch (error) {
      const box = CM.dom.drawerBody.querySelector("[data-avatar-results]");
      if (box) box.insertAdjacentHTML("afterbegin", `<div class="error">${CM.escapeHtml(error.message)}</div>`);
      buttons.forEach(button => { button.disabled = false; });
      if (selected) selected.textContent = "选这个";
    }
  }

  CM.dom.headerAvatar.classList.add("avatar-editable");
  CM.dom.headerAvatar.title = "设置头像";
  CM.dom.headerAvatar.addEventListener("click", () => {
    if (!CM.isGroupConversation()) open().catch(console.error);
  });

  CM.dom.drawerBody.addEventListener("click", event => {
    if (event.target.closest("[data-avatar-search]")) { search().catch(console.error); return; }
    if (event.target.closest("[data-avatar-search-again]")) { open(managerCharacterId).catch(console.error); return; }
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
  CM.registerFeature("avatars", {open, refreshProfiles, apply:applyAvatars});
})();