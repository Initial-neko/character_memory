(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before character_archive.js");

  let archived = [];

  // Archive browsing is a list-level action, but it should not compete with the
  // character list title. Put a quiet, explicit entry directly below "新建人物"
  // so it remains discoverable without consuming the sidebar header.
  const archiveListButton = document.createElement("button");
  archiveListButton.className = "character-archive-entry character-archive-list-button";
  archiveListButton.type = "button";
  archiveListButton.title = "查看已归档人物";
  archiveListButton.innerHTML = '<span class="character-archive-entry-icon" aria-hidden="true">▣</span><span class="character-archive-entry-label">查看归档人物</span><span class="character-archive-entry-chevron" aria-hidden="true">›</span>';
  const createButton = document.querySelector(".create-character-button");
  if (createButton) createButton.insertAdjacentElement("afterend", archiveListButton);
  else CM.dom.characterList?.insertAdjacentElement("afterend", archiveListButton);
  const archiveLabel = archiveListButton.querySelector(".character-archive-entry-label");

  function renderArchiveListButton() {
    if (!archiveLabel) return;
    archiveLabel.textContent = archived.length ? `查看归档人物 (${archived.length})` : "查看归档人物";
  }

  async function refreshArchiveCount() {
    const data = await CM.api("/v1/characters?archived=true");
    archived = data.characters || [];
    renderArchiveListButton();
  }

  function closeMenus(exceptId = null) {
    CM.dom.characterList?.querySelectorAll("[data-character-menu]").forEach(menu => {
      if (exceptId && menu.dataset.characterMenu === exceptId) return;
      menu.classList.add("hidden");
    });
  }

  async function reloadCharacters() {
    try {
      await CM.loadCharacters();
    } catch (error) {
      CM.dom.characterList.innerHTML = '<div class="character-archive-empty">全部人物都已归档，可从「查看归档人物」里恢复。</div>';
    }
  }

  async function archiveCharacter(characterId) {
    if (!characterId) return;
    closeMenus();
    await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/archive`, {method:"POST"});
    if (!CM.isGroupConversation() && CM.state.characterId === characterId) {
      const fallback = CM.state.characters.find(item => item.id !== characterId);
      if (fallback) await CM.switchCharacter(fallback.id);
    }
    await reloadCharacters();
    await refreshArchiveCount().catch(console.error);
  }

  function renderArchivedDrawer() {
    if (!archived.length) {
      CM.dom.drawerBody.innerHTML = '<p class="muted">还没有归档的人物。</p>';
      return;
    }
    CM.dom.drawerBody.innerHTML = `<div class="character-archive-list">${archived.map(profile => {
      const archivedAt = profile.archived_at ? CM.fmtDate(profile.archived_at) : "";
      return `<div class="character-archive-card"><div class="character-archive-copy"><strong>${CM.escapeHtml(profile.name || profile.id)}</strong><span>${CM.escapeHtml(profile.identity || profile.tagline || "")}</span>${archivedAt ? `<small>归档于 ${CM.escapeHtml(archivedAt)}</small>` : ""}</div><button type="button" data-character-restore="${CM.escapeHtml(profile.id)}">恢复</button></div>`;
    }).join("")}</div>`;
  }

  async function showArchived() {
    CM.openDrawer("已归档人物", "归档只隐藏人物，不删除聊天记录、Memory、Trace 或媒体；可随时恢复");
    CM.dom.drawerBody.innerHTML = "<p>正在读取归档人物…</p>";
    try {
      const data = await CM.api("/v1/characters?archived=true");
      archived = data.characters || [];
      renderArchivedDrawer();
      renderArchiveListButton();
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  }

  async function restoreCharacter(characterId) {
    if (!characterId) return;
    await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/restore`, {method:"POST"});
    archived = archived.filter(item => item.id !== characterId);
    await reloadCharacters();
    renderArchivedDrawer();
    renderArchiveListButton();
  }

  CM.dom.characterList?.addEventListener("click", event => {
    const more = event.target.closest("[data-character-more]");
    if (more) {
      event.stopPropagation();
      const id = more.dataset.characterMore;
      const menu = CM.dom.characterList.querySelector(`[data-character-menu="${CSS.escape(id)}"]`);
      const wasHidden = menu?.classList.contains("hidden");
      closeMenus(id);
      if (menu && wasHidden) menu.classList.remove("hidden");
      return;
    }
    if (!event.target.closest("[data-character]")) closeMenus();
  });

  CM.dom.characterList?.addEventListener("click", event => {
    const archive = event.target.closest("[data-character-archive]");
    if (archive) archiveCharacter(archive.dataset.characterArchive).catch(console.error);
  });

  CM.dom.drawerBody.addEventListener("click", event => {
    const restore = event.target.closest("[data-character-restore]");
    if (restore) restoreCharacter(restore.dataset.characterRestore).catch(console.error);
  });

  archiveListButton.addEventListener("click", () => showArchived().catch(console.error));
  document.addEventListener("click", event => {
    if (!event.target.closest(".character-item-wrap")) closeMenus();
  });

  refreshArchiveCount().catch(() => {});

  CM.registerFeature("characterArchive", {showArchived, archiveCharacter, restoreCharacter, refreshArchiveCount});
})();
