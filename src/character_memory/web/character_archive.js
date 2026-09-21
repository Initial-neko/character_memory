(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before character_archive.js");

  let archived = [];
  const sidebarTitle = document.querySelector(".sidebar-title");

  // The sidebar "Characters" heading is the only always-present anchor for a
  // list-level action, so the archive drawer hangs off it the way the group
  // archive list hangs off its section head.
  if (sidebarTitle) {
    const actions = document.createElement("span");
    actions.className = "character-section-actions";
    actions.innerHTML = '<button class="character-archive-list-button" type="button" title="查看已归档角色">归档</button>';
    sidebarTitle.appendChild(actions);
  }
  const archiveListButton = sidebarTitle?.querySelector(".character-archive-list-button");

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
      // Every character is archived. The "没有发现任何 Persona" throw guards a
      // broken persona tree, not a deliberate empty list, so the hint replaces
      // it here instead of surfacing an error for something the operator did.
      CM.dom.characterList.innerHTML = '<div class="character-archive-empty">全部角色都已归档，可从「归档」里恢复。</div>';
    }
  }

  async function archiveCharacter(characterId) {
    if (!characterId) return;
    closeMenus();
    await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/archive`, {method:"POST"});
    // Switch before reloading: the sidebar must never be left pointing at a
    // character it no longer lists.
    if (!CM.isGroupConversation() && CM.state.characterId === characterId) {
      const fallback = CM.state.characters.find(item => item.id !== characterId);
      if (fallback) await CM.switchCharacter(fallback.id);
    }
    await reloadCharacters();
  }

  function renderArchivedDrawer() {
    if (!archived.length) {
      CM.dom.drawerBody.innerHTML = '<p class="muted">还没有归档的角色。</p>';
      return;
    }
    CM.dom.drawerBody.innerHTML = `<div class="character-archive-list">${archived.map(profile => {
      const archivedAt = profile.archived_at ? CM.fmtDate(profile.archived_at) : "";
      return `<div class="character-archive-card"><div class="character-archive-copy"><strong>${CM.escapeHtml(profile.name || profile.id)}</strong><span>${CM.escapeHtml(profile.identity || profile.tagline || "")}</span>${archivedAt ? `<small>归档于 ${CM.escapeHtml(archivedAt)}</small>` : ""}</div><button type="button" data-character-restore="${CM.escapeHtml(profile.id)}">恢复</button></div>`;
    }).join("")}</div>`;
  }

  async function showArchived() {
    CM.openDrawer("已归档角色", "归档只隐藏角色，不删除聊天记录、Memory、Trace 或媒体；可随时恢复");
    CM.dom.drawerBody.innerHTML = "<p>正在读取归档角色…</p>";
    try {
      const data = await CM.api("/v1/characters?archived=true");
      archived = data.characters || [];
      renderArchivedDrawer();
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

  archiveListButton?.addEventListener("click", () => showArchived().catch(console.error));
  document.addEventListener("click", event => {
    if (!event.target.closest(".character-item-wrap")) closeMenus();
  });

  CM.registerFeature("characterArchive", {showArchived, archiveCharacter, restoreCharacter});
})();
