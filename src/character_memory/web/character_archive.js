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

  // The archive/restore response carries the outcome of the best-effort GSV
  // registry refresh. Archive itself succeeds either way -- the row disappears
  // and the record stays on disk -- so a failed refresh used to be a 200 the
  // archive module threw away, leaving a running GSV sidecar still resolving a
  // character the user had just archived. Surface it instead -- but only the
  // half of it the user can do something about.
  //
  // Three outcomes arrive here, and telling the last two apart is the point:
  //
  //   reloaded     the sidecar took the new roster. Nothing to say.
  //   rejected     the sidecar answered and refused. It is running and it is
  //                still holding the old roster, so the standing warning below
  //                is both actionable and true.
  //   unreachable  nothing answered, or nothing answered in time. Most installs
  //                have no GSV sidecar at all, and a reload queued behind a
  //                synthesis can outlast any timeout. Neither is the user's to
  //                fix, and neither may claim a sidecar is running -- the
  //                standing banner did, on machines that had none.
  function voiceReloadMessage(result) {
    const registry = result?.voice_registry;
    if (!registry || registry.reloaded !== false) return null;
    const reason = String(registry.reason || "").trim();
    // A payload without a status predates this split, and every one of those
    // meant "the refresh did not happen". Fall through to the refusal case so
    // an older API can only over-report, never go silent.
    if (registry.status === "unreachable") {
      return {
        persistent: false,
        text: `没有连上 GSV 语音服务${reason ? `（${CM.escapeHtml(reason)}）` : ""}，语音名单这次没有刷新。人物列表已经更新；GSV 下次启动时会自己读到新名单。`,
      };
    }
    return {
      persistent: true,
      text: `语音注册表没有刷新${reason ? `：${CM.escapeHtml(reason)}` : ""}。人物列表已经更新，但运行中的 GSV sidecar 可能仍按旧名单合成语音；修好 voices 模板后重新归档或恢复一次，或重启 GSV sidecar。`,
    };
  }

  // The warning gets a channel of its own, because it is a message and not a
  // flow change. Archiving closes the drawer on every path -- that is what the
  // confirm card is for -- and holding it open just to have somewhere to put
  // the text leaves a full-viewport backdrop over the sidebar: the next archive
  // or restore is unreachable until the user finds the one button that dismisses
  // it. So the notice is pinned to the page, above the drawer and transparent to
  // the pointer, and the drawer closes exactly as it did before the reload
  // result was read at all.
  //
  // It is also never permanent, whatever the outcome. It used to clear only on
  // the next clean reload, which on a deployment that never has a sidecar is
  // never -- one strip, fixed at top:84px, for the rest of the session. Now the
  // refusal carries a real dismiss button, and the unreachable case clears
  // itself, so no outcome can leave it parked over the sidebar.
  const VOICE_NOTICE_TRANSIENT_MS = 8000;
  const voiceNotice = document.createElement("div");
  voiceNotice.className = "archive-voice-notice hidden";
  voiceNotice.setAttribute("role", "status");
  document.body.appendChild(voiceNotice);
  let voiceNoticeTimer = null;

  function hideVoiceNotice() {
    if (voiceNoticeTimer) {
      clearTimeout(voiceNoticeTimer);
      voiceNoticeTimer = null;
    }
    voiceNotice.classList.add("hidden");
    voiceNotice.innerHTML = "";
  }

  function showVoiceReloadWarning(result) {
    hideVoiceNotice();
    const message = voiceReloadMessage(result);
    if (!message) return;
    // The container stays pointer-events:none so it cannot shadow a control
    // underneath it; the button is the one thing that opts back in.
    voiceNotice.innerHTML = `<span class="archive-voice-notice-text">${message.text}</span><button type="button" class="archive-voice-notice-close" data-archive-voice-dismiss aria-label="关闭提示" title="关闭提示">✕</button>`;
    voiceNotice.classList.remove("hidden");
    if (!message.persistent) voiceNoticeTimer = setTimeout(hideVoiceNotice, VOICE_NOTICE_TRANSIENT_MS);
  }

  voiceNotice.addEventListener("click", event => {
    if (event.target.closest("[data-archive-voice-dismiss]")) hideVoiceNotice();
  });

  async function archiveCharacter(characterId) {
    if (!characterId) return;
    if (CM.state.characters.length <= 1) {
      const profile = profileFor(characterId);
      showLastCharacterGuard(profile);
      return;
    }

    const fallback = CM.state.characters.find(item => item.id !== characterId) || null;
    const result = await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/archive`, {method:"POST"});

    if (!CM.isGroupConversation() && CM.state.characterId === characterId && fallback) {
      await CM.switchCharacter(fallback.id);
    }
    await reloadCharacters();
    await refreshArchiveCount().catch(console.error);
    showVoiceReloadWarning(result);
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
    const capacity = CM.state.characterCapacity || {activeTotal:CM.state.characters.length,softLimit:10,hardLimit:20};
    if (Number(capacity.activeTotal || 0) >= Number(capacity.hardLimit || 20)) {
      throw new Error(`角色已达到 ${capacity.hardLimit || 20} 位上限，请先归档一位人物。`);
    }
    const needsConfirm = Number(capacity.activeTotal || 0) >= Number(capacity.softLimit || 10);
    if (needsConfirm && !window.confirm(`当前已有 ${capacity.activeTotal} 位角色。恢复后会超过 10 位提醒阈值，是否继续？`)) return;
    const query = needsConfirm ? "?confirm_over_soft_limit=true" : "";
    const result = await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/restore${query}`, {method:"POST"});
    archived = archived.filter(item => item.id !== characterId);
    await reloadCharacters();
    renderArchivedDrawer();
    renderArchiveListButton();
    showVoiceReloadWarning(result);
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
