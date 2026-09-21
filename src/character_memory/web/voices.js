(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before voices.js");

  const STATUS_COPY = {
    set: "已设置",
    unset: "使用默认声线",
    missing: "模板已丢失",
    error: "配置有误",
  };

  // The TTS Lab is a separate origin. There is no base helper for it -- the
  // repo hardcodes cross-origin links the same way index.html links Settings
  // Center -- so a literal with a name is clearer than an inline URL.
  const LAB_BASE = "http://127.0.0.1:9002";

  function profileFor(characterId) {
    return CM.state.characters.find(item => item.id === characterId) || null;
  }

  function optionsHtml(current, names) {
    const known = names.map(
      name => `<option value="${CM.escapeHtml(name)}" ${name === current ? "selected" : ""}>${CM.escapeHtml(name)}</option>`
    );
    if (current && !names.includes(current)) {
      known.unshift(`<option value="${CM.escapeHtml(current)}" selected>${CM.escapeHtml(current)}（已丢失）</option>`);
    }
    return [`<option value="">使用默认声线</option>`, ...known].join("");
  }

  async function loadSnapshot() {
    // One call carries both halves: every template (with who references it) and
    // every character's status. Same-origin through CM.api, like the other
    // per-character calls on this page -- the TTS Lab is a different origin and
    // a relative fetch could not reach it.
    const snapshot = await CM.api("/v1/voice-templates");
    return {
      templates: snapshot.templates || [],
      characters: snapshot.characters || {},
      errors: snapshot.errors || [],
    };
  }

  async function refresh() {
    // Re-read after a write so the picker and the "who else uses this" count are
    // never stale. The sidecar reloads its own registry server-side, so there is
    // nothing to push from here.
    return await loadSnapshot();
  }

  function render(characterId, entry, snapshot) {
    const status = entry?.status || "unset";
    const names = snapshot.templates.map(item => item.name);
    const used = snapshot.templates.find(item => item.name === entry?.template)?.used_by || [];
    const others = used.filter(id => id !== characterId);

    const errorLine = entry?.error
      ? `<p class="voice-panel-error">${CM.escapeHtml(entry.error)}</p>`
      : "";
    // Overwriting a template changes everyone pointing at it, so say so before
    // the user picks one -- this is the drawer half of the freeze warning.
    const sharedLine = others.length
      ? `<p class="voice-panel-shared">另有 ${others.length} 个角色（${CM.escapeHtml(others.join("、"))}）在用这个声音。</p>`
      : "";
    const hint = status === "unset"
      ? `<p class="muted">当前使用默认声线。到声音合成页设计一个，或用下面的下拉选一个已有模板。</p>`
      : "";
    const errorsLine = snapshot.errors.length
      ? `<p class="voice-panel-error">有模板不可用：${CM.escapeHtml(snapshot.errors.join("；"))}</p>`
      : "";

    CM.dom.drawerBody.innerHTML = `
      <section class="voice-panel">
        <div class="voice-panel-status">
          <span class="voice-panel-badge" data-status="${CM.escapeHtml(status)}">${CM.escapeHtml(STATUS_COPY[status] || status)}</span>
          <span class="voice-panel-current">${CM.escapeHtml(entry?.template || "—")}</span>
        </div>
        ${errorLine}
        ${errorsLine}
        <label class="voice-panel-picker">
          <span>选择声线模板</span>
          <select data-voice-template>${optionsHtml(entry?.template, names)}</select>
        </label>
        ${sharedLine}
        ${hint}
        <div class="voice-panel-actions">
          <button type="button" class="primary" data-voice-save>保存</button>
          <a class="voice-panel-link" href="${LAB_BASE}/tts" target="_blank" rel="noopener noreferrer">去声音合成页造一个</a>
        </div>
        <p class="voice-panel-result" data-voice-result></p>
      </section>`;

    const save = CM.dom.drawerBody.querySelector("[data-voice-save]");
    const select = CM.dom.drawerBody.querySelector("[data-voice-template]");
    const result = CM.dom.drawerBody.querySelector("[data-voice-result]");
    save.addEventListener("click", async () => {
      save.disabled = true;
      result.textContent = "保存中…";
      try {
        const chosen = select.value || null;
        await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/voice`, {
          method: "POST",
          body: JSON.stringify({template: chosen}),
        });
        const snapshot = await refresh();
        render(characterId, snapshot.characters[characterId], snapshot);
      } catch (error) {
        result.textContent = `保存失败：${error.message}`;
      } finally {
        save.disabled = false;
      }
    });
  }

  async function open(characterId = CM.state.characterId) {
    if (CM.isGroupConversation()) return;
    const profile = profileFor(characterId);
    CM.openDrawer(
      `${profile?.name || characterId} · 声线`,
      "选一个模板，或到声音合成页设计一个新的"
    );
    CM.dom.drawerBody.innerHTML = "<p>正在加载…</p>";
    try {
      const snapshot = await refresh();
      render(characterId, snapshot.characters[characterId], snapshot);
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  }

  function wireEntryPoint() {
    // Sibling of the avatar panel, which is opened from the header avatar.
    if (!CM.dom.characterName) return;
    if (!CM.dom.characterName.dataset.voiceWired) {
      CM.dom.characterName.dataset.voiceWired = "1";
      CM.dom.characterName.classList.add("voice-editable");
      CM.dom.characterName.addEventListener("click", () => {
        if (!CM.isGroupConversation()) open().catch(console.error);
      });
    }
    // group_settings.js renames the group from this same element and owns the
    // title while a group is open, so a group is left entirely alone -- not
    // even to clear the attribute, which would erase its tooltip.
    if (!CM.isGroupConversation()) CM.dom.characterName.title = "设置声线";
  }

  CM.on("conversationChanged", wireEntryPoint);
  CM.on("charactersLoaded", wireEntryPoint);
  wireEntryPoint();

  CM.registerFeature("voice", {open, refresh});
})();
