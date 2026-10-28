/* Relationship practice: one SIMULATED scene, 2-3 partners, the shared composer.
 *
 * This module owns only the practice surface. Drafts come from the existing
 * reply-suggestion feature and sending goes through the same composer, so the
 * user edits/free-writes exactly as in Direct chat. Every accepted turn is
 * written by the server with a frozen SIMULATED scope; this file never talks to
 * the memory or direct-chat endpoints.
 */
(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before scenarios.js");

  const section = document.getElementById("sidebarScenario");
  if (!section) throw new Error("sidebar scenario slot is missing");
  section.innerHTML = '<button type="button" class="sidebar-source-link scenario-entry" title="在模拟情境里和 2–3 位伙伴练习；不写入现实记忆">◇ <span>关系练习</span></button>';

  const STATUS = {
    CREATED: "未开始",
    ACTIVE: "进行中",
    PAUSED: "已暂停",
    ENDED: "已结束",
  };

  let view = null;
  let scenarios = [];
  let drawerError = "";
  let formOpen = false;
  let creating = false;
  let pendingCreate = null;
  let resumeAttempt = null;
  let retry = null;
  let entryGeneration = 0;
  let loadGeneration = 0;
  let hintOriginal = null;

  const activeId = () => CM.isScenarioConversation() ? CM.state.conversation.activityId : null;
  const isActiveId = id => Boolean(id) && activeId() === id && CM.isScenarioConversation();
  const esc = CM.escapeHtml;
  const currentStatus = () => view?.activity?.status || "";
  const latestEvent = () => view?.scenario?.events?.at(-1) || null;

  function participantName(item) {
    if (item.actor_type === "CHARACTER") {
      return CM.state.characters.find(profile => profile.id === item.character_id)?.name
        || item.display_name || item.character_id;
    }
    return item.display_name || item.local_id;
  }

  function participantKey(item) {
    return item.actor_type === "CHARACTER" ? item.character_id : "sim:" + item.local_id;
  }

  function nameByKey(key) {
    return participantName((view?.activity?.participants || []).find(item => participantKey(item) === key) || {});
  }

  function setHint() {
    const hint = document.querySelector(".composer-hint");
    if (!hint) return;
    if (hintOriginal === null) hintOriginal = hint.textContent;
    hint.textContent = "Enter 发送 · Shift+Enter 换行 · 练习内容只留在这次情境里";
  }

  function restoreHint() {
    const hint = document.querySelector(".composer-hint");
    if (hint && hintOriginal !== null) hint.textContent = hintOriginal;
  }

  function notify(text, {pending = false} = {}) {
    const note = document.createElement("div");
    note.className = "chat-note" + (pending ? " scenario-pending" : "");
    note.setAttribute("role", "status");
    note.innerHTML = `<strong>${pending ? "正在等伙伴回应" : "这一轮没有完成"}</strong><span>${esc(text)}</span>`;
    CM.dom.chat.appendChild(note);
    CM.scrollToBottom(false);
    return note;
  }

  function clearPending() {
    CM.dom.chat.querySelector(".scenario-pending")?.remove();
  }

  function roundNote(round) {
    if (!round || round.status === "READY") return "";
    if (round.status === "PENDING") {
      return `<div class="chat-note scenario-round-pending"><strong>这一轮停在半途</strong><span>结果没有确认，也没有补写任何伙伴内容。</span><button type="button" class="detail-button" data-scenario-abandon="${esc(round.request_id)}">结束这一轮</button></div>`;
    }
    const messages = {
      SCENARIO_GENERATION_FAILED: "伙伴回应没有生成。只保留了你说的这句话，没有替任何人补写观点。",
      SCENARIO_STATE_CHANGED: "这一轮开始后练习状态变了，结果没有写入。",
      SCENARIO_ROUND_UNRESOLVED: "这一轮结果未确认，已记为未完成，没有生成伙伴内容。",
    };
    return `<div class="chat-note"><strong>这一轮没有完成</strong><span>${esc(messages[round.failure_code] || "没有生成伙伴观点或回应。")}</span></div>`;
  }

  function renderTurns(scenario) {
    const rounds = new Map((scenario.rounds || []).map(round => [round.turn_id, round]));
    const turns = [];
    const byId = new Map();
    for (const event of scenario.events || []) {
      let turn = byId.get(event.turn_id);
      if (!turn) {
        turn = {turn_id:event.turn_id, events:[]};
        byId.set(event.turn_id, turn);
        turns.push(turn);
      }
      turn.events.push(event);
    }
    return turns.map(turn => {
      const user = turn.events.find(event => event.event_type === "USER_MESSAGE");
      const perspectives = turn.events.filter(event => event.event_type === "PERSPECTIVE");
      const replies = turn.events.filter(event => event.event_type === "REPLY");
      const partnerFacts = perspectives.length || replies.length;
      const userHtml = user ? `<article class="message-row user" data-scenario-event="${user.id}"><div class="bubble-wrap"><div class="bubble">${esc(user.content)}</div><div class="message-meta"><span>${esc(CM.fmtTime(user.created_at))}</span></div></div></article>` : "";
      const views = perspectives.length ? `<section class="scenario-perspectives" aria-label="伙伴观点"><div class="scenario-perspectives-head">每个人看到的不一样</div><div class="scenario-views">${perspectives.map(item => `<article class="scenario-view"><header><span class="scenario-view-avatar">${esc(nameByKey(item.participant_key).slice(0, 1))}</span><span class="scenario-view-name">${esc(nameByKey(item.participant_key))}</span><span class="scenario-stance">${esc(item.stance || "自己的看法")}</span></header><p>${esc(item.content)}</p></article>`).join("")}</div></section>` : "";
      const replyHtml = replies.map(item => `<article class="message-row assistant group-assistant scenario-reply" data-scenario-event="${item.id}"><div class="avatar">${esc(nameByKey(item.participant_key).slice(0, 1))}</div><div class="bubble-wrap"><div class="group-speaker-name">${esc(nameByKey(item.participant_key))} · 模拟回应</div><div class="bubble">${esc(item.content)}</div><div class="message-meta"><span>${esc(CM.fmtTime(item.created_at))}</span></div></div></article>`).join("");
      const round = rounds.get(turn.turn_id);
      const note = partnerFacts ? "" : roundNote(round);
      return userHtml + note + views + replyHtml;
    }).join("");
  }

  function banner() {
    const activity = view.activity;
    const status = activity.status;
    const statusLabel = STATUS[status] || status;
    const notes = view.scenario.notes || {};
    const cast = (activity.participants || []).map(item => {
      const note = notes[participantKey(item)] || (item.actor_type === "CHARACTER" ? "正式人物 · 只提供视角" : "");
      return `<span class="scenario-cast-item">${esc(participantName(item))}${note ? `<small>${esc(note)}</small>` : ""}</span>`;
    }).join("");
    const canResume = status === "PAUSED" || status === "CREATED";
    return `<section class="scenario-brief" aria-label="练习情境">
      <div class="scenario-brief-head"><div><span class="scenario-mode-chip">模拟情境</span><span class="scenario-status is-${esc(status.toLowerCase())}">${esc(statusLabel)}</span></div><div class="scenario-brief-actions">${canResume ? `<button type="button" class="detail-button" data-scenario-resume>${status === "CREATED" ? "开始练习" : "继续练习"}</button>` : ""}<button type="button" class="detail-button" data-scenario-exit>退出练习</button></div></div>
      <h2>${esc(activity.title)}</h2>
      <dl><dt>情境</dt><dd>${esc(view.scenario.situation)}</dd><dt>想练</dt><dd>${esc(view.scenario.goal)}</dd>${view.scenario.concern ? `<dt>顾虑</dt><dd>${esc(view.scenario.concern)}</dd>` : ""}</dl>
      <div class="scenario-cast" aria-label="参与者">${cast}</div>
      <p class="scenario-scope-note">这是模拟练习，不会写入现实记忆；伙伴的回应也不是现实里已经发生的事。</p>
    </section>`;
  }

  function render() {
    if (!view) return;
    const body = renderTurns(view.scenario);
    CM.dom.chat.innerHTML = banner() + (body || '<div class="scenario-empty">先写下你在这个情境里想说的第一句话。</div>');
    CM.scrollToBottom(false);
  }

  async function loadHistory() {
    const id = activeId();
    if (!id) return;
    const generation = ++loadGeneration;
    try {
      const data = await CM.api(`/v2/scenarios/${encodeURIComponent(id)}`);
      if (generation !== loadGeneration || !isActiveId(id)) return;
      view = data;
      if (view.activity.status !== "CREATED") resumeAttempt = null;
      render();
      CM.updateHeader();
      await CM.emit("historyLoaded", {type:"SCENARIO", activityId:id});
    } catch (error) {
      if (generation !== loadGeneration || !isActiveId(id)) return;
      view = null;
      CM.dom.chat.innerHTML = '<div class="error chat-send-error">练习记录暂时读不出来。没有生成新的伙伴内容，可以稍后重新打开。</div>';
      CM.updateHeader();
    }
  }

  function applyHeader() {
    if (!CM.isScenarioConversation()) return false;
    CM.dom.headerAvatar.textContent = "练";
    CM.dom.runtimeButton.disabled = true;
    CM.dom.runtimeButton.title = "模拟练习没有现实人物运行时";
    CM.features.intent?.setDisabled?.(true);
    if (!view) {
      CM.dom.characterName.textContent = "关系练习";
      CM.dom.characterIdentity.textContent = "正在读取模拟情境…";
      CM.dom.input.placeholder = "正在打开练习…";
      return true;
    }
    const status = currentStatus();
    CM.dom.characterName.textContent = view.activity.title;
    // The cast is visible in the scene card; the header keeps the boundary
    // readable at 400px instead of truncating "不写入现实记忆" behind names.
    CM.dom.characterIdentity.textContent = `模拟练习 · 不写入现实记忆${status === "ACTIVE" ? "" : " · " + (STATUS[status] || status)}`;
    CM.dom.input.placeholder = status === "ACTIVE"
      ? "写下你在这个情境里的回应"
      : status === "ENDED" ? "这次练习已经结束" : "先继续这次练习";
    return true;
  }

  function applyComposerState() {
    if (!CM.isScenarioConversation()) return false;
    const active = currentStatus() === "ACTIVE";
    CM.dom.sendButton.disabled = !active || CM.state.sendingCount > 0;
    CM.dom.input.disabled = !active;
    CM.features.stickers?.setDisabled?.(true);
    CM.features.images?.setDisabled?.(true);
    CM.features.intent?.setDisabled?.(true);
    if (active) CM.dom.input.focus();
    return true;
  }

  function leave() {
    if (!CM.isScenarioConversation()) return;
    entryGeneration += 1;
    loadGeneration += 1;
    view = null;
    retry = null;
    resumeAttempt = null;
    CM.state.conversation = {type:"DIRECT", groupId:null};
    document.body.classList.remove("scenario-mode");
    restoreHint();
    CM.renderCharacterList();
  }

  async function enter(activityId, {start = true} = {}) {
    const generation = ++entryGeneration;
    CM.features.groups?.leave?.();
    CM.closeDirectStream?.();
    CM.state.conversation = {type:"SCENARIO", groupId:null, activityId};
    CM.state.lastRenderedSignature = "";
    document.body.classList.add("scenario-mode");
    setHint();
    view = null;
    retry = null;
    CM.closeDrawer();
    CM.features.stickers?.close?.();
    CM.features.images?.close?.();
    CM.renderCharacterList();
    CM.updateHeader();
    CM.dom.chat.innerHTML = '<div class="empty">正在打开这次练习…</div>';
    await loadHistory();
    if (generation !== entryGeneration || !isActiveId(activityId)) return;
    if (start && currentStatus() === "CREATED") await resumePractice();
    if (generation !== entryGeneration || !isActiveId(activityId)) return;
    CM.updateHeader();
    await CM.emit("conversationChanged", {type:"SCENARIO", activityId});
  }

  async function resumePractice() {
    const id = activeId();
    if (!id || !view || currentStatus() === "ACTIVE" || currentStatus() === "ENDED") return;
    const version = view.activity.version;
    if (!resumeAttempt || resumeAttempt.version !== version) {
      resumeAttempt = {version, id:crypto.randomUUID()};
    }
    const attempt = resumeAttempt;
    try {
      await CM.api(`/v2/activities/${encodeURIComponent(id)}/resume`, {
        method:"POST",
        body:JSON.stringify({request_id:attempt.id, expected_version:version}),
      });
      if (!isActiveId(id)) return;
      resumeAttempt = null;
      await loadHistory();
    } catch (error) {
      if (!isActiveId(id)) return;
      const status = statusOf(error);
      if (status === 409) {
        // The state changed while we were asking; reload shows the truth.
        await loadHistory();
        if (currentStatus() !== "ACTIVE") notify("这次练习没能继续，状态已经变化；请重新打开查看。");
      } else {
        notify("这次练习没能继续。没有生成新的伙伴内容，可以稍后再试。");
      }
    }
  }

  function statusOf(error) {
    const match = /^(\d{3})\b/.exec(String(error?.message || ""));
    return match ? Number(match[1]) : 0;
  }

  function detailOf(error) {
    const text = String(error?.message || "");
    const index = text.indexOf(": ");
    if (index < 0) return "";
    try {
      return String(JSON.parse(text.slice(index + 2))?.detail || "");
    } catch {
      return "";
    }
  }

  function restoreDraft(message) {
    if (CM.dom.input.value.trim()) return;
    CM.dom.input.value = message;
    CM.dom.input.dispatchEvent(new Event("input", {bubbles:true}));
  }

  async function sendText(message, attribution) {
    const id = activeId();
    if (!id || !view || !message) return;
    if (currentStatus() !== "ACTIVE") {
      CM.dom.input.value = message;
      notify("这次练习现在不能发送。先继续练习，再写你的回应。");
      CM.updateComposerState();
      return;
    }
    if (message.length > 4000) {
      notify("这句话太长了（最多 4000 字），还没有发送。");
      return;
    }
    const body = {
      request_id: retry?.message === message ? retry.id : crypto.randomUUID(),
      message,
    };
    if (attribution) body.reply_suggestion = attribution;
    CM.dom.input.value = "";
    CM.dom.input.style.height = "auto";
    CM.dom.input.dispatchEvent(new Event("input", {bubbles:true}));
    CM.beginSending();
    notify("每个人会给出自己的看法，模拟人物再按自己的身份回应。", {pending:true});
    try {
      await CM.api(`/v2/scenarios/${encodeURIComponent(id)}/messages`, {
        method:"POST",
        body:JSON.stringify(body),
      });
      if (!isActiveId(id)) return;
      retry = null;
      await loadHistory();
    } catch (error) {
      if (!isActiveId(id)) return;
      clearPending();
      const status = statusOf(error);
      const detail = detailOf(error);
      const accepted = status === 502 || detail === "SCENARIO_STATE_CHANGED";
      const previousPending = detail === "SCENARIO_ROUND_PENDING";
      if (status === 502) {
        // The server durably recorded the accepted turn and its failed round.
        // Do not restore the draft: the sentence is already in the transcript.
        retry = null;
        await loadHistory();
        CM.scrollToBottom(false);
        return;
      }
      if (previousPending) {
        retry = null;
        await loadHistory();
        return;
      }
      if (status === 409) {
        if (detail === "SCENARIO_STATE_CHANGED") {
          // The round was accepted, then the scene changed before commit. Its
          // failed receipt is durable; reload rather than reviving the draft.
          retry = null;
          await loadHistory();
          return;
        }
        // Not accepted (paused/ended scene or a stale draft attribution). The
        // user's text is still theirs to retry after the state is clear.
        retry = null;
        restoreDraft(message);
        if (detail === "SCENARIO_NOT_ACTIVE") {
          await loadHistory();
          notify("这次练习已暂停或结束，句子已放回输入框；先继续练习再发送。");
        } else {
          notify("这次没有发送：练习状态或建议批次已经变化。句子已放回输入框，可以重新组织后再发。");
        }
        return;
      }
      if (status === 404) {
        retry = null;
        notify("这次练习已经不存在，句子没有发送。");
        return;
      }
      // Unknown outcome: keep the same request id so a retry cannot duplicate
      // the accepted turn, and put the sentence back in the composer.
      retry = {id:body.request_id, message};
      restoreDraft(message);
      if (accepted) {
        await loadHistory();
      } else if (status) {
        notify("没能确认这一轮是否送达。句子已放回输入框；再发一次会沿用同一个请求，不会重复。");
      } else {
        notify("发送结果还没有确认。句子已放回输入框；再发一次会沿用同一个请求，不会重复。");
      }
    } finally {
      if (isActiveId(id)) CM.endSending();
    }
  }

  function statusPill(item) {
    const status = item.activity.status;
    return `<span class="scenario-status is-${esc(status.toLowerCase())}">${esc(STATUS[status] || status)}</span>`;
  }

  function renderDrawer() {
    const list = scenarios.length ? `<div class="scenario-list">${scenarios.map(item => {
      const activity = item.activity;
      const names = (activity.participants || []).map(participantName).join("、");
      const turns = item.scenario.rounds?.length || 0;
      const current = isActiveId(activity.id) ? " current" : "";
      return `<button type="button" class="scenario-card${current}" data-scenario-open="${esc(activity.id)}"><span class="scenario-card-copy"><strong>${esc(activity.title)}</strong><small>${esc(names)} · ${turns} 轮</small></span>${statusPill(item)}</button>`;
    }).join("")}</div>` : '<p class="scenario-empty-copy">还没有练习。创建一个情境，和 2–3 位伙伴各说各的看法。</p>';
    const form = formOpen ? createForm() : "";
    CM.dom.drawerBody.innerHTML = `<div class="scenario-drawer">
      <div class="scenario-drawer-head"><div><h3>我的练习</h3><p>观点与回应只留在这次模拟情境里，不写入现实记忆。</p></div><button type="button" class="scenario-new" data-scenario-new>＋ 新建练习</button></div>
      ${drawerError ? `<p class="form-error" role="alert">${esc(drawerError)}</p>` : ""}
      ${list}${form}
    </div>`;
    CM.dom.drawerBody.querySelector("[data-scenario-new]")?.addEventListener("click", () => {
      formOpen = true;
      renderDrawer();
      CM.dom.drawerBody.querySelector(".scenario-create-form")?.scrollIntoView({block:"nearest"});
    });
    CM.dom.drawerBody.querySelectorAll("[data-scenario-open]").forEach(button => {
      button.addEventListener("click", () => enter(button.dataset.scenarioOpen).catch(console.error));
    });
    bindForm();
  }

  function createForm() {
    const formal = (CM.state.characters || []).map(profile => `<label class="scenario-formal-option"><input type="checkbox" data-scenario-formal value="${esc(profile.id)}" ${profile.id === CM.state.characterId ? "checked" : ""}><span><strong>${esc(profile.name || profile.id)}</strong><small>${esc(profile.identity || profile.tagline || "正式人物")}</small></span></label>`).join("");
    return `<form class="scenario-create-form" data-scenario-form novalidate>
      <div class="scenario-form-head"><h3>创建一个情境</h3><span>共 2–3 位伙伴</span></div>
      <div class="scenario-form-grid">
        <label>标题<input type="text" data-scenario-field="title" maxlength="160" placeholder="例如：和家里说想换工作" autocomplete="off"></label>
        <label>发生了什么<textarea data-scenario-field="situation" maxlength="2000" rows="2" placeholder="正在发生什么，谁在场，你想在哪件事上练习。"></textarea></label>
        <label>你想练什么<textarea data-scenario-field="goal" maxlength="1000" rows="2" placeholder="例如：把计划说清楚，也听见她们的担心。"></textarea></label>
        <label>你担心什么（可选）<textarea data-scenario-field="concern" maxlength="1000" rows="2" placeholder="例如：怕一开口就变成辩解。"></textarea></label>
      </div>
      <fieldset class="scenario-cast-field"><legend>正式人物（1–2 位）</legend><p class="scenario-hint">从已有的人物里选；他们只给出自己的视角。</p><div class="scenario-formal-picker">${formal}</div></fieldset>
      <fieldset class="scenario-cast-field"><legend>模拟人物（1–2 位）</legend><p class="scenario-hint">比如妈妈、同事。只在这次练习里存在，不会变成新的 Person。</p>
        <div class="scenario-sim-row"><label>称呼<input type="text" data-scenario-sim-name="0" maxlength="60" placeholder="例如：妈妈" autocomplete="off"></label><label>她/他会在意什么<textarea data-scenario-sim-description="0" maxlength="400" rows="2" placeholder="例如：担心收入不稳定，习惯先问最坏的情况。"></textarea></label></div>
        <div class="scenario-sim-row"><label>称呼（可选）<input type="text" data-scenario-sim-name="1" maxlength="60" placeholder="例如：姐姐" autocomplete="off"></label><label>她/他会在意什么<textarea data-scenario-sim-description="1" maxlength="400" rows="2" placeholder="例如：务实，愿意一起想步骤。"></textarea></label></div>
      </fieldset>
      <p class="form-error scenario-form-error" role="alert" data-scenario-form-error></p>
      <div class="scenario-create-actions"><button type="button" class="detail-button" data-scenario-cancel>取消</button><button type="submit" class="scenario-submit" ${creating ? "disabled" : ""}>${creating ? "正在创建…" : "创建并开始"}</button></div>
    </form>`;
  }

  function readForm(form) {
    const field = name => form.querySelector(`[data-scenario-field="${name}"]`).value.trim();
    const formals = [...form.querySelectorAll("[data-scenario-formal]:checked")].map(input => input.value);
    const sims = [0, 1].map(index => ({
      name: form.querySelector(`[data-scenario-sim-name="${index}"]`).value.trim(),
      description: form.querySelector(`[data-scenario-sim-description="${index}"]`).value.trim(),
      index,
    })).filter(item => item.name || item.description);
    return {
      title: field("title"), situation: field("situation"), goal: field("goal"),
      concern: field("concern"), formals, sims,
    };
  }

  function formError(form, text) {
    const node = form.querySelector("[data-scenario-form-error]");
    if (node) node.textContent = text;
  }

  function bindForm() {
    const form = CM.dom.drawerBody.querySelector("[data-scenario-form]");
    if (!form) return;
    form.querySelector("[data-scenario-cancel]")?.addEventListener("click", () => {
      formOpen = false;
      renderDrawer();
    });
    form.querySelectorAll("[data-scenario-formal]").forEach(input => {
      input.addEventListener("change", () => {
        const checked = [...form.querySelectorAll("[data-scenario-formal]:checked")];
        if (checked.length > 2) {
          input.checked = false;
          formError(form, "正式人物最多选 2 位。");
        } else {
          formError(form, "");
        }
      });
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (creating) return;
      const values = readForm(form);
      if (!values.title || !values.situation || !values.goal) {
        formError(form, "标题、情境和想练什么都需要写。");
        return;
      }
      if (!values.formals.length || values.formals.length > 2) {
        formError(form, "请选择 1–2 位正式人物。");
        return;
      }
      if (!values.sims.length || values.sims.length > 2) {
        formError(form, "请写 1–2 位模拟人物。");
        return;
      }
      if (values.sims.some(item => !item.name)) {
        formError(form, "模拟人物需要称呼。");
        return;
      }
      if (values.formals.length + values.sims.length < 2 || values.formals.length + values.sims.length > 3) {
        formError(form, "正式人物和模拟人物合计需要 2–3 位。");
        return;
      }
      const participants = [
        ...values.formals.map(character_id => ({actor_type:"CHARACTER", character_id})),
        ...values.sims.map(item => ({actor_type:"SIMULATED_CHARACTER", local_id:`p${item.index + 1}`, display_name:item.name})),
      ];
      const descriptions = Object.fromEntries(values.sims.filter(item => item.description)
        .map(item => [`sim:p${item.index + 1}`, item.description]));
      const signature = JSON.stringify([values.title, values.situation, values.goal, values.concern, participants, descriptions]);
      if (!pendingCreate || pendingCreate.signature !== signature) {
        pendingCreate = {signature, body:{
          request_id:crypto.randomUUID(), title:values.title, situation:values.situation,
          goal:values.goal, concern:values.concern || null, participants, descriptions,
        }};
      }
      creating = true;
      formError(form, "");
      form.querySelector("button[type=submit]").disabled = true;
      form.querySelector("button[type=submit]").textContent = "正在创建…";
      try {
        const created = await CM.api("/v2/scenarios", {method:"POST", body:JSON.stringify(pendingCreate.body)});
        pendingCreate = null;
        creating = false;
        await enter(created.activity.id);
      } catch (error) {
        creating = false;
        const status = statusOf(error);
        formError(form, status === 422
          ? "这份设置没有被接受，请检查人物和文字后重试。"
          : status ? "创建没有成功，输入还在；可以再试一次。" : "创建结果还没有确认；再试会沿用同一个请求，不会重复创建。");
        form.querySelector("button[type=submit]").disabled = false;
        form.querySelector("button[type=submit]").textContent = "创建并开始";
      }
    });
  }

  async function openDrawer() {
    formOpen = false;
    drawerError = "";
    CM.openDrawer("关系练习", "在模拟情境里和 2–3 位伙伴练习；内容不会写入现实记忆");
    CM.dom.drawerBody.innerHTML = '<p class="empty">正在读取练习…</p>';
    try {
      const data = await CM.api("/v2/scenarios?limit=50");
      scenarios = data.scenarios || [];
      if (!scenarios.length) formOpen = true;
      renderDrawer();
    } catch (error) {
      drawerError = "练习列表暂时读不出来。";
      scenarios = [];
      formOpen = true;
      renderDrawer();
    }
  }

  section.querySelector(".scenario-entry")?.addEventListener("click", () => openDrawer().catch(console.error));

  CM.dom.chat.addEventListener("click", event => {
    if (!CM.isScenarioConversation()) return;
    const resume = event.target.closest("[data-scenario-resume]");
    if (resume) { resumePractice().catch(console.error); return; }
    const exit = event.target.closest("[data-scenario-exit]");
    if (exit) { CM.switchCharacter(CM.state.characterId).catch(console.error); return; }
    const abandon = event.target.closest("[data-scenario-abandon]");
    if (abandon) {
      const id = activeId();
      if (!id) return;
      abandon.disabled = true;
      CM.api(`/v2/scenarios/${encodeURIComponent(id)}/rounds/${encodeURIComponent(abandon.dataset.scenarioAbandon)}/abandon`, {method:"POST", body:"{}"})
        .then(() => loadHistory())
        .catch(error => notify(error?.message?.includes("404") ? "这一轮已经不在练习记录里。" : "没能结束这一轮，记录没有变化。"))
        .finally(() => { if (isActiveId(id)) CM.updateComposerState(); });
    }
  });

  CM.registerFeature("scenarios", {
    loadHistory, enter, leave, applyHeader, applyComposerState, sendText, latestEvent,
  });
})();
