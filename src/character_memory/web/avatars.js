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
    const image = imageHtml(profile);
    const marker = `${profile.id}|${profile.avatar_url || ""}`;
    if (container.dataset.avatarMarker === marker) return;
    container.dataset.avatarMarker = marker;
    container.innerHTML = image || CM.escapeHtml(CM.initialFor(profile));
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
            host.dataset.avatarMarker = "";
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

  function defaultQuery(profile) {
    return [profile?.name || profile?.id || "", profile?.identity || "", "portrait avatar profile picture"].filter(Boolean).join(" ").trim();
  }

  function managerHtml(profile) {
    const avatar = imageHtml(profile, "avatar-manager-current-image");
    return `<div class="avatar-manager">
      <section class="avatar-manager-current">
        <div class="avatar-manager-current-preview">${avatar || `<span>${CM.escapeHtml(CM.initialFor(profile))}</span>`}</div>
        <div><strong>${CM.escapeHtml(profile.name || profile.id)}</strong><p>${profile.avatar_url ? "当前头像已保存到本地。" : "当前还没有头像，将继续使用首字母。"}</p></div>
      </section>
      <label class="avatar-search-field"><span>搜索关键词</span><input type="text" data-avatar-query maxlength="400" value="${CM.escapeHtml(defaultQuery(profile))}"></label>
      <div class="avatar-manager-actions"><button class="primary" type="button" data-avatar-search>搜索头像</button></div>
      <div data-avatar-results class="avatar-results"><p class="muted">搜索结果会先作为候选展示，只有你选择后才会下载并设为头像。</p></div>
    </div>`;
  }

  async function open(characterId = CM.state.characterId) {
    const profile = profileById(characterId);
    if (!profile) return;
    managerCharacterId = characterId;
    currentSearch = null;
    CM.openDrawer(`${profile.name || profile.id} · 头像`, "Image Search 只用于寻找候选；确认后才保存到本地");
    CM.dom.drawerBody.innerHTML = managerHtml(profile);
    CM.dom.drawerBody.querySelector("[data-avatar-query]")?.focus();
  }

  function renderCandidates(result) {
    const box = CM.dom.drawerBody.querySelector("[data-avatar-results]");
    if (!box) return;
    const candidates = result?.candidates || [];
    if (!candidates.length) {
      box.innerHTML = '<div class="error">没有找到适合作为头像的图片，可以换一组关键词再试。</div>';
      return;
    }
    box.innerHTML = `<div class="avatar-search-summary">找到 ${candidates.length} 个候选 · ${CM.escapeHtml(result.query || "")}</div><div class="avatar-candidate-grid">${candidates.map(item => {
      const size = item.width && item.height ? `${item.width}×${item.height}` : "尺寸未知";
      return `<article class="avatar-candidate"><div class="avatar-candidate-image"><img src="${CM.escapeHtml(item.thumbnail_url)}" alt="${CM.escapeHtml(item.title || "头像候选")}" loading="lazy"></div><div class="avatar-candidate-copy"><strong>${CM.escapeHtml(item.title || "头像候选")}</strong><span>${CM.escapeHtml(item.source_domain || "未知来源")} · ${CM.escapeHtml(size)}</span></div><div class="avatar-candidate-actions"><a href="${CM.escapeHtml(item.source_page_url)}" target="_blank" rel="noopener noreferrer">来源</a><button type="button" data-avatar-select="${CM.escapeHtml(item.id)}">选这个</button></div></article>`;
    }).join("")}</div>`;
  }

  async function search() {
    if (!managerCharacterId) return;
    const query = CM.dom.drawerBody.querySelector("[data-avatar-query]")?.value.trim() || "";
    const button = CM.dom.drawerBody.querySelector("[data-avatar-search]");
    const box = CM.dom.drawerBody.querySelector("[data-avatar-results]");
    if (button) { button.disabled = true; button.textContent = "正在搜索…"; }
    if (box) box.innerHTML = '<p class="muted">正在从图片搜索中寻找头像候选…</p>';
    try {
      currentSearch = await CM.api(`/v1/characters/${encodeURIComponent(managerCharacterId)}/avatar/search`, {method:"POST", body:JSON.stringify({query, limit:12})});
      renderCandidates(currentSearch);
    } catch (error) {
      if (box) box.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    } finally {
      if (button) { button.disabled = false; button.textContent = "搜索头像"; }
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
    if (!event.target.closest("[data-avatar-query]") || event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    search().catch(console.error);
  });

  const observer = new MutationObserver(() => applyAvatars());
  observer.observe(CM.dom.chat, {childList:true, subtree:true});
  observer.observe(CM.dom.characterList, {childList:true, subtree:true});

  CM.on("charactersLoaded", () => refreshProfiles().catch(error => console.warn("avatar profile refresh failed", error)));
  CM.on("historyLoaded", applyAvatars);
  CM.on("conversationChanged", applyAvatars);
  CM.registerFeature("avatars", {open, refreshProfiles, apply:applyAvatars});
})();
