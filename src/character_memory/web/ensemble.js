(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before ensemble.js");

  const section = document.getElementById("sidebarGroups");
  const actions = section?.querySelector(".group-section-actions");
  if (!section || !actions) return;

  const button = document.createElement("button");
  button.className = "ensemble-create-button";
  button.type = "button";
  button.title = "一句话按资料创建多个角色并组成群聊";
  button.textContent = "AI";
  actions.insertBefore(button, actions.firstChild);

  let currentBuild = null;

  function introHtml() {
    return `
      <div class="ensemble-builder">
        <div class="ensemble-intro">
          <strong>一句话创建一整组人物</strong>
          <p>例如：“复刻命运石之门的 LAB MEM，并形成群聊”。系统会先创建群，再读取公开资料、生成成员草稿，最后只让你确认一次。</p>
        </div>
        <label class="ensemble-prompt-field">
          <span>你想创建什么群？</span>
          <textarea data-ensemble-prompt rows="5" maxlength="2000" placeholder="复刻命运石之门的 LAB MEM，并形成群聊"></textarea>
        </label>
        <div class="ensemble-actions">
          <button type="button" data-ensemble-cancel>取消</button>
          <button type="button" class="primary" data-ensemble-start>开始整理</button>
        </div>
        <div class="ensemble-error hidden" data-ensemble-error></div>
      </div>
    `;
  }

  function openBuilder() {
    currentBuild = null;
    CM.openDrawer("AI 建群", "先建群，再查资料，最后只确认一次");
    CM.dom.drawerBody.innerHTML = introHtml();
  }

  function showError(message) {
    const box = CM.dom.drawerBody.querySelector("[data-ensemble-error]");
    if (!box) return;
    box.textContent = message;
    box.classList.remove("hidden");
  }

  function sourceHtml(build) {
    const sources = Array.isArray(build.sources) ? build.sources : [];
    if (!sources.length) return "";
    return `
      <details class="ensemble-sources">
        <summary>查看公开资料来源（${sources.length}）</summary>
        <div class="ensemble-source-list">
          ${sources.map(source => `
            <a href="${CM.escapeHtml(source.url || "")}" target="_blank" rel="noreferrer">
              <strong>${CM.escapeHtml(source.title || source.domain || "公开资料")}</strong>
              <span>${CM.escapeHtml(source.domain || "")}</span>
            </a>
          `).join("")}
        </div>
      </details>
    `;
  }

  function memberHtml(item) {
    const draft = item.draft || {};
    const existing = Boolean(item.existing_character_id);
    return `
      <label class="ensemble-member-card">
        <input type="checkbox" data-ensemble-member value="${Number(item.index)}" checked>
        <span class="ensemble-member-avatar">${CM.escapeHtml(CM.initialFor(draft))}</span>
        <span class="ensemble-member-copy">
          <span class="ensemble-member-name">
            <strong>${CM.escapeHtml(draft.name || item.canonical_name || "角色")}</strong>
            ${existing ? '<em>已存在 · 直接加入</em>' : '<em>将创建</em>'}
          </span>
          <span>${CM.escapeHtml(draft.identity || item.identity || draft.tagline || "")}</span>
          <small>${CM.escapeHtml((item.relationship_notes || []).slice(0, 2).join("；"))}</small>
        </span>
      </label>
    `;
  }

  function selectedItems(build) {
    const selected = new Set(
      [...CM.dom.drawerBody.querySelectorAll("[data-ensemble-member]:checked")]
        .map(input => Number(input.value))
    );
    return (build.drafts || []).filter(item => selected.has(Number(item.index)));
  }

  function updateCapacity(build) {
    const selected = selectedItems(build);
    const newCount = selected.filter(item => !item.existing_character_id).length;
    const active = Number(build.capacity?.active_count ?? CM.state.characterCapacity?.activeTotal ?? CM.state.characters.length);
    const soft = Number(build.capacity?.soft_limit ?? 10);
    const hard = Number(build.capacity?.hard_limit ?? 20);
    const result = active + newCount;
    const summary = CM.dom.drawerBody.querySelector("[data-ensemble-capacity]");
    const confirm = CM.dom.drawerBody.querySelector("[data-ensemble-confirm]");
    const count = CM.dom.drawerBody.querySelector("[data-ensemble-selected-count]");
    if (count) count.textContent = `已选择 ${selected.length} 位，其中新建 ${newCount} 位`;
    if (summary) {
      summary.classList.toggle("warning", result > soft && result <= hard);
      summary.classList.toggle("blocked", result > hard);
      summary.innerHTML = result > hard
        ? `当前 ${active} 位角色，本次需要新建 ${newCount} 位，结果将是 <strong>${result}/${hard}</strong>。请减少勾选人数。`
        : result > soft
          ? `创建后将是 <strong>${result}/${hard}</strong> 位角色，已经超过 10 位提醒阈值；本页确认一次即可继续。`
          : `创建后将是 <strong>${result}/${hard}</strong> 位角色。`;
    }
    if (confirm) confirm.disabled = selected.length < 2 || selected.length > 12 || result > hard;
  }

  function renderConfirmation(build) {
    currentBuild = build;
    const drafts = Array.isArray(build.drafts) ? build.drafts : [];
    CM.openDrawer(build.group_name || build.group?.name || "确认群成员", "勾选要加入的角色，然后一次确认创建");
    CM.dom.drawerBody.innerHTML = `
      <div class="ensemble-confirmation">
        <div class="ensemble-confirm-head">
          <div>
            <span class="ensemble-kicker">GROUP READY</span>
            <h3>${CM.escapeHtml(build.group_name || build.group?.name || "新群聊")}</h3>
            <p>${CM.escapeHtml(build.overview || "资料已经整理完成。")}</p>
          </div>
          <span class="ensemble-group-state">群已创建 · 待确认成员</span>
        </div>
        <div class="ensemble-capacity" data-ensemble-capacity></div>
        <div class="ensemble-selected-count" data-ensemble-selected-count></div>
        <div class="ensemble-member-list">
          ${drafts.map(memberHtml).join("")}
        </div>
        ${sourceHtml(build)}
        <div class="ensemble-error hidden" data-ensemble-error></div>
        <div class="ensemble-actions sticky">
          <button type="button" data-ensemble-discard>取消这个群</button>
          <button type="button" class="primary" data-ensemble-confirm>确认并开始群聊</button>
        </div>
      </div>
    `;
    updateCapacity(build);
  }

  async function startBuild() {
    const prompt = CM.dom.drawerBody.querySelector("[data-ensemble-prompt]")?.value.trim() || "";
    if (!prompt) {
      showError("先写一句你想创建的群聊。");
      return;
    }
    const start = CM.dom.drawerBody.querySelector("[data-ensemble-start]");
    if (start) { start.disabled = true; start.textContent = "正在建群…"; }
    try {
      const created = await CM.api("/v1/ensembles", {
        method:"POST",
        body:JSON.stringify({prompt}),
      });
      currentBuild = created.build;
      await CM.features.groups?.loadGroups?.();
      CM.openDrawer(currentBuild.group_name || "正在构建群聊", "群已经创建，正在读取公开资料和生成成员草稿");
      CM.dom.drawerBody.innerHTML = `
        <div class="ensemble-loading">
          <span class="ensemble-loading-mark">◎</span>
          <strong>正在整理群成员…</strong>
          <p>正在搜索公开资料、浏览页面并生成各自独立的 Persona 草稿。群聊已经先创建好了。</p>
          <button type="button" data-ensemble-discard>取消这个群</button>
          <div class="ensemble-error hidden" data-ensemble-error></div>
        </div>
      `;
      const researched = await CM.api(`/v1/ensembles/${encodeURIComponent(currentBuild.group_id)}/research`, {method:"POST"});
      renderConfirmation(researched.build);
      await CM.features.groups?.loadGroups?.();
    } catch (error) {
      showError(error.message);
    } finally {
      if (start) { start.disabled = false; start.textContent = "开始整理"; }
    }
  }

  async function discardBuild() {
    if (!currentBuild?.group_id) {
      CM.closeDrawer();
      return;
    }
    try {
      await CM.api(`/v1/ensembles/${encodeURIComponent(currentBuild.group_id)}/cancel`, {method:"POST"});
    } finally {
      currentBuild = null;
      await CM.features.groups?.loadGroups?.();
      CM.closeDrawer();
    }
  }

  async function confirmBuild() {
    if (!currentBuild?.group_id) return;
    const selected = selectedItems(currentBuild);
    if (selected.length < 2) {
      showError("至少保留两位群成员。");
      return;
    }
    const newCount = selected.filter(item => !item.existing_character_id).length;
    const active = Number(currentBuild.capacity?.active_count ?? CM.state.characterCapacity?.activeTotal ?? CM.state.characters.length);
    const soft = Number(currentBuild.capacity?.soft_limit ?? 10);
    const hard = Number(currentBuild.capacity?.hard_limit ?? 20);
    const result = active + newCount;
    if (result > hard) {
      showError(`最多只能保留 ${hard} 位正式角色，请减少勾选人数。`);
      return;
    }
    const confirm = CM.dom.drawerBody.querySelector("[data-ensemble-confirm]");
    if (confirm) { confirm.disabled = true; confirm.textContent = "正在创建角色…"; }
    try {
      const response = await CM.api(`/v1/ensembles/${encodeURIComponent(currentBuild.group_id)}/confirm`, {
        method:"POST",
        body:JSON.stringify({
          selected_indices:selected.map(item => Number(item.index)),
          confirm_over_soft_limit:result > soft,
        }),
      });
      currentBuild = response.build;
      await CM.loadCharacters();
      await CM.features.groups?.loadGroups?.();
      const groupId = currentBuild.group_id;
      CM.closeDrawer();
      await CM.features.groups?.enter?.(groupId);
    } catch (error) {
      if (confirm) { confirm.disabled = false; confirm.textContent = "确认并开始群聊"; }
      showError(error.message);
    }
  }

  button.addEventListener("click", openBuilder);

  CM.dom.drawerBody.addEventListener("change", event => {
    if (event.target.closest("[data-ensemble-member]") && currentBuild) updateCapacity(currentBuild);
  });

  CM.dom.drawerBody.addEventListener("click", event => {
    if (event.target.closest("[data-ensemble-cancel]")) { CM.closeDrawer(); return; }
    if (event.target.closest("[data-ensemble-start]")) { startBuild().catch(console.error); return; }
    if (event.target.closest("[data-ensemble-discard]")) { discardBuild().catch(console.error); return; }
    if (event.target.closest("[data-ensemble-confirm]")) confirmBuild().catch(console.error);
  });

  CM.registerFeature("ensemble", {open:openBuilder});
})();
