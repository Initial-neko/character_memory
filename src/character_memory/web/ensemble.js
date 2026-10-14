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
  let useVoiceDesignRequested = false;
  let lastPrompt = "";
  let lastMode = "SOURCE";
  let preparingTemplate = false;
  let lastScene = {background:"", characters:"", relationships:"", opening:""};

  function introHtml() {
    return `
      <div class="ensemble-builder">
        <div class="ensemble-intro">
          <strong>先体验一个温暖异世界</strong>
          <p>灯溪镇的旅店主人、炼金学徒、巡林人和乐师，各有自己的关注与关心方式。先看四人的原创设定，确认后创建专属人物和群聊。</p>
          <div class="ensemble-actions">
            <button type="button" data-ensemble-template="warm-isekai" title="预览固定原创设定；确认后新建人物，不复用同名角色">预览温暖异世界</button>
          </div>
        </div>
        <div class="ensemble-intro">
          <strong>一句话创建一整组人物</strong>
          <p>例如：“复刻命运石之门的 LAB MEM，并形成群聊”。系统先读取公开资料并准备成员草稿；只有你确认后，才真正创建角色和群聊。</p>
        </div>
        <label class="ensemble-prompt-field">
          <span>你想创建什么群？</span>
          <textarea data-ensemble-prompt rows="5" maxlength="2000" placeholder="复刻命运石之门的 LAB MEM，并形成群聊"></textarea>
        </label>
        <label class="ensemble-voice-option">
          <span>创建类型</span>
          <select data-ensemble-mode><option value="SOURCE">作品 / 公开资料复刻</option><option value="ORIGINAL">原创世界与人物（不联网检索）</option><option value="SCENE">自定义情景（新建专属人物）</option></select>
        </label>
        <div data-ensemble-scene-fields hidden>
          <label class="ensemble-prompt-field"><span>背景与氛围</span><textarea data-ensemble-scene-background rows="3" maxlength="500" placeholder="例如：雨后的小镇旅店，温暖、轻松"></textarea></label>
          <label class="ensemble-prompt-field"><span>希望有哪些人物</span><textarea data-ensemble-scene-characters rows="3" maxlength="600" placeholder="至少两位，写身份、关注或表达特点，也可以请系统创作"></textarea></label>
          <label class="ensemble-prompt-field"><span>关系期待（可选）</span><textarea data-ensemble-scene-relationships rows="2" maxlength="300" placeholder="例如：大家熟悉，欢迎远来的我，各有不同的好感表达"></textarea></label>
          <label class="ensemble-prompt-field"><span>开场</span><textarea data-ensemble-scene-opening rows="2" maxlength="400" placeholder="例如：我走进旅店，可以帮忙、聊天或安静休息"></textarea></label>
        </div>
        <label class="ensemble-voice-option">
          <input type="checkbox" data-ensemble-voice-design>
          <span>
            为新角色尝试生成专属音色
            <small>可选 · 只有你已手动启动 Qwen3 VoiceDesign (:9015) 时才会执行；失败不会影响建群。</small>
          </span>
        </label>
        <div class="ensemble-actions">
          <button type="button" data-ensemble-cancel>取消</button>
          <button type="button" class="primary" data-ensemble-start>开始整理</button>
        </div>
        <div class="ensemble-error hidden" data-ensemble-error></div>
      </div>
    `;
  }

  function restoreSceneFields() {
    const isScene = CM.dom.drawerBody.querySelector("[data-ensemble-mode]")?.value === "SCENE";
    const fields = CM.dom.drawerBody.querySelector("[data-ensemble-scene-fields]");
    const freePrompt = CM.dom.drawerBody.querySelector("[data-ensemble-prompt]")?.closest("label");
    if (fields) fields.hidden = !isScene;
    if (freePrompt) { freePrompt.hidden = isScene; freePrompt.classList.toggle("hidden", isScene); }
    for (const [key, value] of Object.entries(lastScene)) {
      const field = CM.dom.drawerBody.querySelector(`[data-ensemble-scene-${key}]`);
      if (field) field.value = value;
    }
  }

  async function openBuilder() {
    currentBuild = null;
    CM.openDrawer("AI 建群", "正在检查上次保留的构建进度");
    CM.dom.drawerBody.innerHTML = `
      <div class="ensemble-loading">
        <span class="ensemble-loading-mark">◎</span>
        <strong>正在恢复上次进度…</strong>
      </div>
    `;
    let restoredBuild = null;
    try {
      const response = await CM.api("/v1/ensembles");
      restoredBuild = response?.build || null;
    } catch (error) {
      console.warn("ensemble.resume failed", error);
    }
    if (restoredBuild?.status === "READY") {
      renderConfirmation(restoredBuild);
      return;
    }
    if (restoredBuild?.status === "FAILED") {
      renderBuildFailure(restoredBuild);
      return;
    }
    CM.openDrawer("AI 建群", "一句话整理资料和成员草稿，确认一次后再真正创建群聊");
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
    const template = item.generation_method === "TEMPLATE";
    const failed = item.status === "FAILED" || !item.draft;
    if (failed) {
      return `
        <div class="ensemble-member-card ensemble-member-failed">
          <span class="ensemble-member-avatar">!</span>
          <span class="ensemble-member-copy">
            <span class="ensemble-member-name">
              <strong>${CM.escapeHtml(item.canonical_name || "角色")}</strong>
              <em>暂未整理成功</em>
            </span>
            <span>这一位已跳过，不影响其他成员继续建群。</span>
            <small>可以只重试这一位，不会重新搜索整个群。</small>
          </span>
          <button type="button" data-ensemble-retry-member="${Number(item.index)}">重试</button>
        </div>
      `;
    }
    const previewFields = [
      ["人物描述", draft.description], ["性格", (draft.personality || []).join("；")],
      ["对话习惯", draft.conversation], ["情绪表达", draft.expression],
      ["追问", draft.questions], ["沉默", draft.silence],
      ["主动", draft.initiative], ["分歧", draft.disagreement],
      ["关心", draft.care], ["边界", (draft.boundaries || []).join("；")],
    ];
    return `
      <div class="ensemble-member-card">
        <input type="checkbox" data-ensemble-member value="${Number(item.index)}" checked aria-label="选择 ${CM.escapeHtml(draft.name || item.canonical_name || "角色")}">
        <span class="ensemble-member-avatar">${CM.escapeHtml(CM.initialFor(draft))}</span>
        <span class="ensemble-member-copy">
          <span class="ensemble-member-name">
            <strong>${CM.escapeHtml(draft.name || item.canonical_name || "角色")}</strong>
            ${existing ? '<em>已存在 · 保留当前设定</em>' : template ? '<em>原创设定 · 新建专属人物</em>' : item.generation_method === "FALLBACK" ? '<em>基础草稿 · 深化失败可重试</em>' : '<em>已深化 · 将创建</em>'}
          </span>
          <span>${CM.escapeHtml(draft.identity || item.identity || draft.tagline || "")}</span>
          <small>${CM.escapeHtml((item.relationship_notes || []).slice(0, 2).join("；"))}</small>
          <details class="ensemble-persona-preview">
            <summary>${existing ? "查看研究草稿（实际复用现有人物设定）" : "查看完整人物草稿"}</summary>
            ${previewFields.map(([title, value]) => `<p><strong>${title}：</strong>${CM.escapeHtml(value || "未提供")}</p>`).join("")}
          </details>
          ${existing || template ? "" : `<button type="button" data-ensemble-regenerate="${Number(item.index)}">重新深化此人物</button>`}
        </span>
      </div>
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

  function renderBuildFailure(build) {
    currentBuild = build;
    if (build.mode === "SCENE" && build.scene) lastScene = {...lastScene, ...build.scene};
    CM.openDrawer("AI 建群", "本次进度已保留，可以直接重试，不会创建空群");
    CM.dom.drawerBody.innerHTML = `
      <div class="ensemble-builder">
        <div class="ensemble-intro">
          <strong>这次资料还没有整理完成</strong>
          <p>可能是公开资料、模型结构化输出或临时网络问题。技术错误已留在服务端日志中，不需要你处理格式。</p>
        </div>
        <div class="ensemble-actions">
          <button type="button" data-ensemble-edit>修改描述</button>
          <button type="button" class="primary" data-ensemble-retry-build>重试整理</button>
        </div>
        <div class="ensemble-error hidden" data-ensemble-error></div>
      </div>
    `;
  }

  function renderConfirmation(build, selectedIndices = null) {
    currentBuild = build;
    if (build.mode === "SCENE" && build.scene) lastScene = {...lastScene, ...build.scene};
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
          <span class="ensemble-group-state">草稿已准备 · 尚未创建群聊</span>
        </div>
        <div class="ensemble-capacity" data-ensemble-capacity></div>
        <div class="ensemble-selected-count" data-ensemble-selected-count></div>
        <div class="ensemble-member-list">
          ${drafts.map(memberHtml).join("")}
        </div>
        ${sourceHtml(build)}
        <div class="ensemble-error hidden" data-ensemble-error></div>
        <div class="ensemble-actions sticky">
          <button type="button" data-ensemble-discard>放弃这次构建</button>
          <button type="button" class="primary" data-ensemble-confirm>确认并开始群聊</button>
        </div>
      </div>
    `;
    if (selectedIndices) {
      const selected = new Set(selectedIndices);
      CM.dom.drawerBody.querySelectorAll("[data-ensemble-member]").forEach(input => {
        input.checked = selected.has(Number(input.value));
      });
    }
    updateCapacity(build);
  }

  async function prepareTemplate(templateId) {
    if (preparingTemplate) return;
    preparingTemplate = true;
    currentBuild = null;
    useVoiceDesignRequested = Boolean(CM.dom.drawerBody.querySelector("[data-ensemble-voice-design]")?.checked);
    CM.openDrawer("温暖异世界", "正在准备四位人物的原创设定，确认后再创建");
    CM.dom.drawerBody.innerHTML = '<div class="ensemble-loading"><strong>正在打开人物设定…</strong></div>';
    try {
      const response = await CM.api(`/v1/ensembles/templates/${encodeURIComponent(templateId)}/prepare`, {method:"POST"});
      renderConfirmation(response.build);
    } catch (error) {
      CM.openDrawer("AI 建群", "暂时无法打开情景设定，请稍后重试");
      CM.dom.drawerBody.innerHTML = introHtml();
      showError("情景设定暂时无法打开，尚未创建人物或群聊。");
    } finally {
      preparingTemplate = false;
    }
  }

  async function startBuild() {
    lastMode = CM.dom.drawerBody.querySelector("[data-ensemble-mode]")?.value || "SOURCE";
    const isScene = lastMode === "SCENE";
    if (isScene) {
      lastScene = Object.fromEntries(Object.keys(lastScene).map(key => [key, CM.dom.drawerBody.querySelector(`[data-ensemble-scene-${key}]`)?.value.trim() || ""]));
      if (["background", "characters", "opening"].some(key => lastScene[key].length < 3)) {
        showError("请填写背景、人物和开场，每项至少三个字。");
        return;
      }
    }
    const prompt = CM.dom.drawerBody.querySelector("[data-ensemble-prompt]")?.value.trim() || "";
    if (!prompt && !isScene) {
      showError("先写一句你想创建的群聊。");
      return;
    }
    currentBuild = null;
    lastPrompt = prompt;
    useVoiceDesignRequested = Boolean(
      CM.dom.drawerBody.querySelector("[data-ensemble-voice-design]")?.checked
    );
    CM.openDrawer("正在整理 AI 群聊", "此时只准备资料和人物草稿，不会提前创建空群");
    CM.dom.drawerBody.innerHTML = `
      <div class="ensemble-loading">
        <span class="ensemble-loading-mark">◎</span>
        <strong>正在整理群成员…</strong>
        <p>${lastMode === "SOURCE" ? "正在搜索公开资料、浏览页面并准备人物草稿。" : "正在根据你的描述创作人物与初始关系，不联网检索。"}完成后确认一次再创建。</p>
        <div class="ensemble-error hidden" data-ensemble-error></div>
      </div>
    `;
    try {
      const prepared = await CM.api(isScene ? "/v1/ensembles/scenes/prepare" : "/v1/ensembles/prepare", {
        method:"POST",
        body:JSON.stringify(isScene ? lastScene : {prompt, mode:lastMode}),
      });
      if (prepared.build?.status === "FAILED") {
        renderBuildFailure(prepared.build);
      } else {
        renderConfirmation(prepared.build);
      }
    } catch (error) {
      currentBuild = null;
      CM.openDrawer("AI 建群", "暂时无法开始整理，请稍后重试");
      CM.dom.drawerBody.innerHTML = introHtml();
      const textarea = CM.dom.drawerBody.querySelector("[data-ensemble-prompt]");
      if (textarea) textarea.value = prompt;
      const mode = CM.dom.drawerBody.querySelector("[data-ensemble-mode]");
      if (mode) mode.value = lastMode;
      restoreSceneFields();
      showError("资料整理暂时失败。请稍后重试；如果持续失败，再查看 Dev Console / 服务端日志。");
    }
  }

  async function retryBuild() {
    if (!currentBuild?.group_id) return;
    CM.openDrawer("正在重新整理 AI 群聊", "会复用这次构建记录；不会提前创建角色或群聊");
    CM.dom.drawerBody.innerHTML = `
      <div class="ensemble-loading">
        <span class="ensemble-loading-mark">◎</span>
        <strong>正在重新整理…</strong>
        <p>如果之前只是临时格式或网络问题，这次会直接恢复。</p>
      </div>
    `;
    try {
      const response = await CM.api(`/v1/ensembles/${encodeURIComponent(currentBuild.group_id)}/research`, {method:"POST"});
      if (response.build?.status === "FAILED") renderBuildFailure(response.build);
      else renderConfirmation(response.build);
    } catch (error) {
      renderBuildFailure(currentBuild);
      showError("仍然没有整理成功。可以修改描述后重试，或稍后再试。");
    }
  }

  async function retryMember(index) {
    if (!currentBuild?.group_id) return;
    const selected = selectedItems(currentBuild).map(item => Number(item.index));
    try {
      const response = await CM.api(
        `/v1/ensembles/${encodeURIComponent(currentBuild.group_id)}/members/${Number(index)}/retry`,
        {method:"POST"}
      );
      renderConfirmation(response.build, selected);
    } catch (error) {
      showError("这一位暂时仍无法整理；其他可用成员可以继续创建。");
    }
  }

  async function regenerateMember(index) {
    if (!currentBuild?.group_id) return;
    const selected = selectedItems(currentBuild).map(item => Number(item.index));
    try {
      const response = await CM.api(
        `/v1/ensembles/${encodeURIComponent(currentBuild.group_id)}/members/${Number(index)}/regenerate`,
        {method:"POST"}
      );
      renderConfirmation(response.build, selected);
    } catch (error) {
      showError("此人物的深化暂时失败，其他成员不受影响。");
    }
  }

  function editPrompt() {
    const prompt = currentBuild?.prompt || lastPrompt || "";
    lastMode = currentBuild?.mode || lastMode;
    currentBuild = null;
    CM.openDrawer("AI 建群", "修改一句话描述后重新整理");
    CM.dom.drawerBody.innerHTML = introHtml();
    const textarea = CM.dom.drawerBody.querySelector("[data-ensemble-prompt]");
    const voice = CM.dom.drawerBody.querySelector("[data-ensemble-voice-design]");
    if (textarea) textarea.value = prompt;
    const mode = CM.dom.drawerBody.querySelector("[data-ensemble-mode]");
    if (mode) mode.value = currentBuild?.mode || lastMode;
    restoreSceneFields();
    if (voice) voice.checked = useVoiceDesignRequested;
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
          use_voice_design:useVoiceDesignRequested,
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

  button.addEventListener("click", () => { openBuilder().catch(console.error); });

  CM.dom.drawerBody.addEventListener("change", event => {
    if (event.target.closest("[data-ensemble-mode]")) {
      // Preserve fields when moving between the ordinary and scenario forms.
      for (const key of Object.keys(lastScene)) {
        const field = CM.dom.drawerBody.querySelector(`[data-ensemble-scene-${key}]`);
        if (field) lastScene[key] = field.value;
      }
      restoreSceneFields();
    }
    if (event.target.closest("[data-ensemble-member]") && currentBuild) updateCapacity(currentBuild);
  });

  CM.dom.drawerBody.addEventListener("click", event => {
    const templateButton = event.target.closest("[data-ensemble-template]");
    if (templateButton) { prepareTemplate(templateButton.dataset.ensembleTemplate).catch(console.error); return; }
    if (event.target.closest("[data-ensemble-cancel]")) { CM.closeDrawer(); return; }
    if (event.target.closest("[data-ensemble-start]")) { startBuild().catch(console.error); return; }
    if (event.target.closest("[data-ensemble-retry-build]")) { retryBuild().catch(console.error); return; }
    if (event.target.closest("[data-ensemble-edit]")) { editPrompt(); return; }
    const retryMemberButton = event.target.closest("[data-ensemble-retry-member]");
    if (retryMemberButton) { retryMember(retryMemberButton.dataset.ensembleRetryMember).catch(console.error); return; }
    const regenerateButton = event.target.closest("[data-ensemble-regenerate]");
    if (regenerateButton) { regenerateMember(regenerateButton.dataset.ensembleRegenerate).catch(console.error); return; }
    if (event.target.closest("[data-ensemble-discard]")) { discardBuild().catch(console.error); return; }
    if (event.target.closest("[data-ensemble-confirm]")) confirmBuild().catch(console.error);
  });

  CM.registerFeature("ensemble", {open:() => openBuilder()});
})();
