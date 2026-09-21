(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before character_archive.js");

  let archived = [];

  const characterActions = document.getElementById("characterActions");
  const topbarActions = document.querySelector(".topbar-actions");

  const archiveListButton = document.createElement("button");
  archiveListButton.className = "character-archive-entry character-archive-list-button";
  archiveListButton.type = "button";
  archiveListButton.title = "查看已归档人物";
  archiveListButton.innerHTML = '<span class="character-archive-entry-icon" aria-hidden="true">▣</span><span class="character-archive-entry-label">查看归档人物</span><span class="character-archive-entry-chevron" aria-hidden="true">›</span>';
  (characterActions || CM.dom.characterList).appendChild(archiveListButton);
  const archiveLabel = archiveListButton.querySelector(".character-archive-entry-label");

  const archiveCurrentButton = document.createElement("button");
  archiveCurrentButton.id = "archiveCurrentCharacterButton";
  archiveCurrentButton.className = "ghost-button desktop-only-control";
  archiveCurrentButton.type = "button";
  archiveCurrentButton.textContent = "归档当前人物";
  archiveCurrentButton.title = "归档当前人物";
  topbarActions?.appendChild(archiveCurrentButton);

  function renderArchiveListButton() {
    if (!archiveLabel) return;
    archiveLabel.textContent = archived.length ? `查看归档人物 (${archived.length})` : "查看归档人物";
  }

  function syncCurrentAction() {
    const disabled = CM.isGroupConversation() || CM.state.characters.length <= 1;
    archiveCurrentButton.hidden = CM.isGroupConversation();
    archiveCurrentButton.disabled = disabled;
    archiveCurrentButton.title = CM.state.characters.length <= 1
      ? "至少保留一个未归档人物"
      : "归档当前人物";
  }

  async function refreshArchiveCount() {
    const data = await CM.api("/v1/characters?archived=true");
    archived = data.characters || [];
    renderArchiveListButton();
    syncCurrentAction();
  }

  function closeMenus(exceptId = null) {
    CM.dom.characterList?.querySelectorAll("[data-character-menu]").forEach(menu => {
      if (exceptId && menu.dataset.characterMenu === exceptId) return;
      menu.classList.add("hidden");
    });
  }

  async function reloadCharacters() {
    await CM.loadCharacters();
    syncCurrentAction();
    CM.features.sidebarCollapse?.sync?.();
  }

  function profileFor(characterId) {
    return CM.state.characters.find(item => item.id === characterId) || null;
  }

  function showLastCharacterGuard(profile) {
    CM.openDrawer("至少保留一个人物", "最后一个未归档人物不能归档");
    CM.dom.drawerBody.innerHTML = `
      <section class="archive-confirm-card">
        <strong>${CM.escapeHtml(profile?.name || "当前人物")} 暂时不能归档</strong>
        <p>当前只剩这一个未归档人物。请先恢复其他人物，或者创建一个新人物，再进行归档。</p>
        <div class="archive-confirm-actions"><button type="button" data-character-archive-cancel>知道了</button></div>
      </section>`;
  }

  function requestArchive(characterId) {
    const profile = profileFor(characterId);
    if (!profile) return;
    closeMenus();
    if (CM.state.characters.length <= 1) {
      showLastCharacterGuard(profile);
      return;
    }
    CM.openDrawer(`归档「${profile.name || profile.id}」？`, "归档只隐藏人物，不会删除历史");
    CM.dom.drawerBody.innerHTML = `
      <section class="archive-confirm-card">
        <strong>归档后会从人物列表中隐藏</strong>
        <p>聊天记录、Memory、Trace、头像和空间动态都会保留。之后可以通过「查看归档人物」随时恢复。</p>
        <div class="archive-confirm-actions">
          <button type="button" data-character-archive-cancel>取消</button>
          <button class="primary" type="button" data-character-archive-confirm="${CM.escapeHtml(characterId)}">归档人物</button>
        </div>
      </section>`;
  }

  async function archiveCharacter(characterId) {
    if (!characterId) return;
    if (CM.state.characters.length <= 1) {
      const profile = profileFor(characterId);
      showLastCharacterGuard(profile);
      return;
    }

    const fallback = CM.state.characters.find(item => item.id !== characterId) || null;
    await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/archive`, {method:"POST"});

    if (!CM.isGroupConversation() && CM.state.characterId === characterId && fallback) {
      await CM.switchCharacter(fallback.id);
    }
    await reloadCharacters();
    await refreshArchiveCount().catch(console.error);
    CM.closeDrawer();
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
    CM.openDrawer("已归档人物", "这里可以恢复人物；聊天、Memory、Trace、头像和空间动态都仍然保留");
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

    const archive = event.target.closest("[data-character-archive]");
    if (archive) {
      event.preventDefault();
      event.stopPropagation();
      requestArchive(archive.dataset.characterArchive);
      return;
    }

    if (!event.target.closest("[data-character]")) closeMenus();
  });

  CM.dom.drawerBody.addEventListener("click", event => {
    if (event.target.closest("[data-character-archive-cancel]")) {
      CM.closeDrawer();
      return;
    }
    const confirm = event.target.closest("[data-character-archive-confirm]");
    if (confirm) {
      confirm.disabled = true;
      archiveCharacter(confirm.dataset.characterArchiveConfirm)
        .catch(error => {
          confirm.disabled = false;
          CM.dom.drawerBody.insertAdjacentHTML("afterbegin", `<div class="error">${CM.escapeHtml(error.message)}</div>`);
        });
      return;
    }
    const restore = event.target.closest("[data-character-restore]");
    if (restore) restoreCharacter(restore.dataset.characterRestore).catch(console.error);
  });

  archiveListButton.addEventListener("click", () => showArchived().catch(console.error));
  archiveCurrentButton.addEventListener("click", () => {
    if (!CM.isGroupConversation()) requestArchive(CM.state.characterId);
  });

  document.addEventListener("click", event => {
    if (!event.target.closest(".character-item-wrap")) closeMenus();
  });

  CM.on("charactersLoaded", syncCurrentAction);
  CM.on("conversationChanged", syncCurrentAction);
  refreshArchiveCount().catch(() => {});
  syncCurrentAction();

  CM.registerFeature("characterArchive", {
    showArchived,
    requestArchive,
    archiveCharacter,
    restoreCharacter,
    refreshArchiveCount,
    syncCurrentAction,
  });
})();
