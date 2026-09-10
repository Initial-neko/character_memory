(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before intent.js");

  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost-button";
  button.textContent = "Intent";
  button.title = "查看人物当前和历史 Intent";
  CM.dom.runtimeButton.insertAdjacentElement("beforebegin", button);

  const formatTime = value => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  };
  const statusText = status => ({PENDING:"等待中",PROCESSING:"执行中",EXECUTED:"已执行",SUPPRESSED:"已放弃",DEFERRED:"已延后",EXPIRED:"已过期",ERROR:"执行失败"})[status] || status || "未知";

  function cards(intents) {
    if (!intents.length) return '<p class="muted">这个人物还没有产生过 Intent。</p>';
    return `<div class="intent-list">${intents.map(item => `<article class="intent-card intent-${CM.escapeHtml(String(item.status || "").toLowerCase())}"><div class="intent-head"><span class="intent-status">${CM.escapeHtml(statusText(item.status))}</span><span>#${CM.escapeHtml(item.id)}</span></div><div class="intent-content">${CM.escapeHtml(item.content)}</div><div class="intent-times"><span>产生：${CM.escapeHtml(formatTime(item.created_at))}</span><span>最早：${CM.escapeHtml(formatTime(item.earliest_at))}</span><span>过期：${CM.escapeHtml(formatTime(item.expires_at))}</span></div>${item.source_event_id ? `<button type="button" class="intent-source" data-intent-source="${CM.escapeHtml(item.source_event_id)}">查看产生它的聊天轮次 · Event #${CM.escapeHtml(item.source_event_id)}</button>` : ""}${item.reason ? `<div class="intent-reason">当时原因：${CM.escapeHtml(item.reason)}</div>` : ""}</article>`).join("")}</div>`;
  }

  async function show() {
    if (CM.isGroupConversation()) return;
    const requested = CM.state.characterId;
    CM.openDrawer(`${CM.currentProfile().name} · Intent`, "模型在聊天时产生的未来行动意图；这里是调试预览，不向人物泄露");
    CM.dom.drawerBody.innerHTML = "<p>正在读取 Intent…</p>";
    try {
      const data = await CM.api(`/v1/runtime/${encodeURIComponent(requested)}`);
      if (CM.isGroupConversation() || requested !== CM.state.characterId) return;
      const intents = data.intents || [];
      const waiting = intents.filter(item => item.status === "PENDING").length;
      CM.dom.drawerBody.innerHTML = `<section class="section"><h3>Intent Preview</h3><div class="intent-summary"><strong>${waiting}</strong> 个等待中的 Intent · 最近共 ${intents.length} 条</div>${cards(intents)}</section><section class="section"><h3>怎么判断问题在哪</h3><p class="muted">完全没有 Intent：模型没有形成未来行动意图。PENDING：还没到时间或仍在等待。SUPPRESSED：到期后决定不发送。EXECUTED：已经形成主动表达。</p></section>`;
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  }

  button.addEventListener("click", show);
  CM.dom.drawerBody.addEventListener("click", event => {
    const source = event.target.closest("[data-intent-source]");
    if (source) CM.showTrace(source.dataset.intentSource);
  });
  CM.registerFeature("intent", {button, show, setDisabled:value => { button.disabled = Boolean(value); }});
})();
