(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before runtime_hardening.js");

  const topbar = document.querySelector(".topbar-actions");
  const wakeButton = document.createElement("button");
  wakeButton.id = "wakeButton";
  wakeButton.className = "ghost-button wake-button";
  wakeButton.type = "button";
  wakeButton.title = "给当前人物一次主动思考机会，不保证一定回复";
  wakeButton.textContent = "唤醒";
  topbar?.insertBefore(wakeButton, CM.dom.runtimeButton || null);

  function toast(text) {
    document.querySelector(".runtime-toast")?.remove();
    const node = document.createElement("div");
    node.className = "runtime-toast";
    node.textContent = text;
    document.body.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
    setTimeout(() => {
      node.classList.remove("show");
      setTimeout(() => node.remove(), 180);
    }, 1800);
  }

  function updateWakeButton() {
    const group = CM.isGroupConversation();
    wakeButton.hidden = group;
    if (!wakeButton.dataset.busy) wakeButton.disabled = group;
  }

  async function reconcileDirectLatest(characterId) {
    if (CM.isGroupConversation() || characterId !== CM.state.characterId) return;
    const params = new URLSearchParams({character_id:characterId, limit:"50"});
    const data = await CM.api(`/v1/chat/history-page?${params.toString()}`);
    if (CM.isGroupConversation() || characterId !== CM.state.characterId) return;
    const byId = new Map(CM.state.directHistory.messages.map(item => [Number(item.id), item]));
    for (const item of data.messages || []) {
      const id = Number(item.id);
      byId.set(id, byId.has(id) ? {...byId.get(id), ...item} : item);
    }
    CM.state.directHistory.messages = [...byId.values()].sort((a,b) => Number(a.id || 0) - Number(b.id || 0));
    CM.state.lastRenderedSignature = "";
    CM.renderHistory(CM.state.directHistory.messages, {preserveScroll:true});
    CM.features.unread?.markRead?.(characterId);
    CM.renderCharacterList();
  }

  // P0.17 reconciliation rule: SQLite history is authoritative. Every new or
  // re-opened SSE connection re-reads the latest durable window and merges by
  // Event id before continuing with ephemeral notifications.
  CM.connectDirectStream = () => {
    if (CM.isGroupConversation()) return;
    const characterId = CM.state.characterId;
    const conversationId = CM.conversationIdFor(characterId);
    const streamKey = `${characterId}:${conversationId}`;
    if (CM.state.directStream && CM.state.directStreamKey === streamKey) return;
    CM.closeDirectStream();
    const params = new URLSearchParams({scope:"direct", character_id:characterId, conversation_id:conversationId});
    const source = new EventSource(`/v1/events/stream?${params.toString()}`);
    CM.state.directStream = source;
    CM.state.directStreamKey = streamKey;
    source.addEventListener("open", () => {
      reconcileDirectLatest(characterId).catch(error => console.warn("[sse reconcile direct]", error));
    });
    source.addEventListener("reaction_status", event => {
      if (CM.isGroupConversation() || characterId !== CM.state.characterId) return;
      const data = JSON.parse(event.data || "{}");
      if (["queued", "typing", "superseded"].includes(data.state)) CM.state.pendingCharacters.add(characterId);
      if (data.state === "idle") CM.state.pendingCharacters.delete(characterId);
      CM.renderCharacterList();
      CM.updateHeader();
      CM.state.lastRenderedSignature = "";
      CM.renderHistory(CM.state.directHistory.messages);
    });
    source.addEventListener("character_event", event => {
      if (CM.isGroupConversation() || characterId !== CM.state.characterId) return;
      const message = CM.directEventToMessage(JSON.parse(event.data));
      if (message.source_event_type === "TIME_TICK") message.proactive = true;
      CM.mergeDirectMessage(message);
      CM.features.unread?.markRead?.(characterId);
    });
    source.addEventListener("reaction_complete", event => {
      if (CM.isGroupConversation() || characterId !== CM.state.characterId) return;
      const data = JSON.parse(event.data || "{}");
      if (data.wake_reason && data.silent) toast(`${CM.currentProfile().name} 醒了一下，没有想说什么`);
    });
    source.addEventListener("reaction_error", event => {
      if (CM.isGroupConversation() || characterId !== CM.state.characterId) return;
      const data = JSON.parse(event.data || "{}");
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `生成失败：${data.message || "未知错误"}`;
      CM.dom.chat.appendChild(box);
    });
  };

  async function manualWake() {
    if (CM.isGroupConversation() || wakeButton.dataset.busy) return;
    const characterId = CM.state.characterId;
    const profile = CM.currentProfile();
    const previous = wakeButton.textContent;
    wakeButton.dataset.busy = "1";
    wakeButton.disabled = true;
    wakeButton.textContent = "唤醒中…";
    CM.connectDirectStream();
    try {
      const result = await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/wake`, {
        method:"POST",
        body:JSON.stringify({conversation_id:CM.conversationIdFor(characterId)}),
      });
      if (!CM.isGroupConversation() && characterId === CM.state.characterId) {
        if (result.silent) toast(`${profile.name || characterId} 醒了一下，没有想说什么`);
        else toast(`${profile.name || characterId} 醒了`);
      }
    } catch (error) {
      toast(`唤醒失败：${error.message}`);
    } finally {
      delete wakeButton.dataset.busy;
      wakeButton.textContent = previous;
      updateWakeButton();
    }
  }

  wakeButton.addEventListener("click", manualWake);
  CM.on("conversationChanged", updateWakeButton);
  CM.on("charactersLoaded", updateWakeButton);

  const json = value => CM.escapeHtml(JSON.stringify(value ?? {}, null, 2));
  const safe = value => CM.escapeHtml(CM.hiddenIfEmpty(value));

  function memoryHtml(trace) {
    const recalled = trace.recalled_memories || [];
    const decisions = trace.memory_decisions || [];
    const recallHtml = recalled.length
      ? `<div class="card-list">${recalled.map(m => `<div class="card"><strong>${CM.escapeHtml(m.memory_type || "Memory")}</strong><span> · importance ${CM.escapeHtml(m.importance)}</span><div>${CM.escapeHtml(m.content || "")}</div></div>`).join("")}</div>`
      : '<p class="muted">本轮没有 Recall 到 Memory。</p>';
    const decisionHtml = decisions.length
      ? `<div class="trace-decision-list">${decisions.map(item => `<div class="trace-decision-row"><strong>${CM.escapeHtml(item.decision || "WRITE")}</strong><span>${CM.escapeHtml(item.candidate?.content || "")}</span>${item.similarity != null ? `<small>similarity ${CM.escapeHtml(item.similarity)}</small>` : ""}</div>`).join("")}</div>`
      : '<p class="muted">没有 Memory Candidate。</p>';
    const created = (trace.created_memory_ids || []).length ? `<p class="muted">写入 Memory ID：${CM.escapeHtml((trace.created_memory_ids || []).join(", "))}</p>` : "";
    return `<h4>Recall</h4>${recallHtml}<h4>Admission</h4>${decisionHtml}${created}`;
  }

  function resourceHtml(trace) {
    const sticker = trace.sticker_retrieval || {};
    const matches = sticker.matches || [];
    const stickerDecisions = trace.sticker_decisions || [];
    const imageDecisions = trace.image_decisions || [];
    if (!matches.length && !stickerDecisions.length && !imageDecisions.length) return '<p class="muted">本轮没有资源选择。</p>';
    return `<div class="kv"><div>Sticker 候选</div><div>${CM.escapeHtml(matches.map(item => item.sticker_id || item.id).filter(Boolean).join(" / ") || "—")}</div><div>Sticker 决策</div><div>${CM.escapeHtml(stickerDecisions.map(item => `${item.sticker_id}:${item.decision}`).join(" / ") || "—")}</div><div>Image 决策</div><div>${CM.escapeHtml(imageDecisions.map(item => `${item.image_id}:${item.decision}`).join(" / ") || "—")}</div></div>`;
  }

  CM.showTrace = async sourceEventId => {
    CM.openDrawer(`${CM.currentProfile().name} · 本轮详情`, `source_event_id = ${sourceEventId}`);
    CM.dom.drawerBody.innerHTML = "<p>正在加载…</p>";
    try {
      const trace = await CM.api(`/v1/traces/${sourceEventId}`);
      const actions = Array.isArray(trace.actions) ? trace.actions : (trace.action ? [trace.action] : []);
      const actionText = actions.length
        ? actions.map((action,index) => `${index + 1}. ${action.type}: ${action.message || action.sticker_id || action.image_id || ""}`).join("\n")
        : "SILENCE";
      const timings = trace.timings || {};
      const summary = `<div class="trace-summary-grid"><div><span>Actions</span><strong>${CM.escapeHtml(actions.map(item => item.type).join(" / ") || "SILENCE")}</strong></div><div><span>Model</span><strong>${CM.escapeHtml(trace.model_used || "—")}</strong></div><div><span>Total</span><strong>${CM.escapeHtml(CM.fmtMs(timings.runtime_total_ms || timings.total_ms || 0))}</strong></div><div><span>Model</span><strong>${CM.escapeHtml(CM.fmtMs(timings.model_ms || trace.model_ms || 0))}</strong></div></div>`;
      const messages = trace.model_messages || [];
      const advancedContext = messages.length
        ? messages.map((m,i) => `<p><strong>${i + 1}. ${CM.escapeHtml(m.role)}</strong></p><pre>${CM.escapeHtml(typeof m.content === "string" ? m.content : JSON.stringify(m.content, null, 2))}</pre>`).join("")
        : `<pre>${safe(trace.context)}</pre>`;
      CM.dom.drawerBody.innerHTML = `
        <section class="section trace-primary"><h3>本轮结果</h3>${summary}<pre>${CM.escapeHtml(actionText)}</pre></section>
        <section class="section"><h3>人物反应</h3><div class="trace-reaction"><div><b>Perception</b><p>${safe(trace.perception)}</p></div><div><b>Reaction</b><p>${safe(trace.reaction)}</p></div></div></section>
        <section class="section"><h3>Mental State</h3><div class="trace-state-flow"><pre>${safe(trace.mental_state_before)}</pre><span>→</span><pre>${safe(trace.mental_state_after)}</pre></div></section>
        <section class="section"><h3>Memory</h3>${memoryHtml(trace)}</section>
        <section class="section"><h3>Intent</h3><pre>${json({candidates:trace.intent_candidates || [], created_intent_ids:trace.created_intent_ids || []})}</pre></section>
        <section class="section"><h3>Sticker / Image</h3>${resourceHtml(trace)}</section>
        <details class="section trace-advanced"><summary>高级调试 · Model Messages / Raw Response</summary><h4>实际发送给模型</h4>${advancedContext}<h4>Raw Model Response</h4><pre>${safe(trace.raw_model_response)}</pre><h4>完整 Timings</h4><pre>${json(timings)}</pre></details>`;
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  };

  CM.registerFeature("runtimeHardening", {manualWake,reconcileDirectLatest,updateWakeButton});
})();
