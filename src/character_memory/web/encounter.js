(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before encounter.js");

  const host = document.querySelector(".space-feed-wrap");
  if (!host) return;

  const section = document.createElement("section");
  section.className = "encounter-section";
  section.innerHTML = `
    <div class="encounter-section-head">
      <div>
        <span class="encounter-kicker">ENCOUNTER</span>
        <h2>今日邂逅</h2>
        <p>偶尔从互联网或这个世界本身遇见一个新的人。</p>
      </div>
      <span class="encounter-capacity" data-encounter-capacity></span>
    </div>
    <div class="encounter-list" data-encounter-list>
      <div class="encounter-empty">正在看看今天会遇见谁…</div>
    </div>
  `;
  host.prepend(section);

  const list = section.querySelector("[data-encounter-list]");
  const capacity = section.querySelector("[data-encounter-capacity]");
  let state = {encounters: [], active_character_count: 0, active_character_limit: 10};

  function sourceLabel(candidate) {
    return candidate.source_type === "WEB" ? "🌐 来自互联网" : "✨ 世界生成";
  }

  function messagesHtml(candidate) {
    const messages = Array.isArray(candidate.messages) ? candidate.messages : [];
    if (!messages.length) return "";
    return messages.map(item => `
      <div class="encounter-message ${item.role === "USER" ? "user" : "encounter-character"}">
        <span>${item.role === "USER" ? "你" : CM.escapeHtml(candidate.draft?.name || "TA")}</span>
        <div>${CM.escapeHtml(item.content)}</div>
      </div>
    `).join("");
  }

  function sourcesHtml(candidate) {
    const urls = Array.isArray(candidate.source_urls) ? candidate.source_urls.slice(0, 2) : [];
    if (!urls.length) return "";
    return `<div class="encounter-sources"><span>灵感来源</span>${urls.map((url, index) => {
      let label = candidate.source_domains?.[index] || url;
      return `<a href="${CM.escapeHtml(url)}" target="_blank" rel="noreferrer">${CM.escapeHtml(label)}</a>`;
    }).join("")}</div>`;
  }

  function cardHtml(candidate) {
    const draft = candidate.draft || {};
    const full = Number(candidate.active_character_count || state.active_character_count) >= Number(candidate.active_character_limit || state.active_character_limit || 10);
    const accepted = candidate.status === "ACCEPTED";
    const closed = ["DISMISSED", "EXPIRED", "FAILED"].includes(candidate.status);
    const canAccept = Boolean(candidate.can_accept) && !full && !accepted && !closed;
    const acceptLabel = accepted ? "已经留下" : full ? "聊天列表已满" : "留下 TA";
    return `
      <article class="encounter-card" data-encounter-id="${candidate.id}">
        <div class="encounter-card-top">
          <span class="encounter-source ${candidate.source_type === "WEB" ? "web" : "generated"}">${sourceLabel(candidate)}</span>
          <span class="encounter-status">${CM.escapeHtml(candidate.status || "NEW")}</span>
        </div>
        <div class="encounter-identity">
          <div class="encounter-avatar">${CM.escapeHtml(CM.initialFor(draft))}</div>
          <div>
            <h3>${CM.escapeHtml(draft.name || "陌生人")}</h3>
            <p>${CM.escapeHtml(draft.identity || draft.tagline || "")}</p>
          </div>
        </div>
        <p class="encounter-hook">${CM.escapeHtml(candidate.encounter_hook || draft.description || "")}</p>
        <blockquote class="encounter-opening">“${CM.escapeHtml(candidate.opening_message || "……你好。")}”</blockquote>
        ${sourcesHtml(candidate)}
        <div class="encounter-actions">
          <button type="button" class="ghost-button" data-encounter-chat="${candidate.id}" ${closed || accepted ? "disabled" : ""}>打个招呼</button>
          <button type="button" class="encounter-keep" data-encounter-accept="${candidate.id}" ${canAccept ? "" : "disabled"}>${acceptLabel}</button>
          <button type="button" class="encounter-dismiss" data-encounter-dismiss="${candidate.id}" ${accepted || closed ? "disabled" : ""}>错过</button>
        </div>
        ${full && !accepted ? `<div class="encounter-limit-note">正式聊天列表最多 ${candidate.active_character_limit || state.active_character_limit || 10} 位。你仍然可以和 TA 临时聊聊；想留下 TA 时先归档一位现有角色。</div>` : ""}
        <div class="encounter-chat-panel ${candidate.status === "CHATTING" ? "" : "hidden"}" data-encounter-panel>
          <div class="encounter-chat-log" data-encounter-log>
            <div class="encounter-message encounter-character"><span>${CM.escapeHtml(draft.name || "TA")}</span><div>${CM.escapeHtml(candidate.opening_message || "")}</div></div>
            ${messagesHtml(candidate)}
          </div>
          <form class="encounter-chat-form" data-encounter-form="${candidate.id}">
            <input type="text" maxlength="4000" placeholder="随便打个招呼…" autocomplete="off">
            <button type="submit">发送</button>
          </form>
          <div class="encounter-error hidden" data-encounter-error></div>
        </div>
      </article>
    `;
  }

  function render() {
    const total = Number(state.active_character_count || 0);
    const limit = Number(state.active_character_limit || 10);
    capacity.textContent = `正式角色 ${Math.min(total, limit)}/${limit}`;
    list.innerHTML = state.encounters.length
      ? state.encounters.map(cardHtml).join("")
      : '<div class="encounter-empty"><strong>今天还没有遇见谁</strong><span>系统会按自己的节奏产生下一次邂逅。</span></div>';
  }

  async function refresh() {
    try {
      const data = await CM.api("/v1/encounters?limit=3&include_closed=false");
      state = data;
      render();
    } catch (error) {
      list.innerHTML = `<div class="encounter-empty"><strong>邂逅读取失败</strong><span>${CM.escapeHtml(error.message)}</span></div>`;
    }
  }

  function errorBox(card, message) {
    const box = card?.querySelector("[data-encounter-error]");
    if (!box) return;
    box.textContent = message;
    box.classList.remove("hidden");
  }

  list.addEventListener("click", async event => {
    const chat = event.target.closest("[data-encounter-chat]");
    if (chat) {
      const card = chat.closest("[data-encounter-id]");
      const panel = card?.querySelector("[data-encounter-panel]");
      panel?.classList.toggle("hidden");
      const id = chat.dataset.encounterChat;
      try { await CM.api(`/v1/encounters/${id}/seen`, {method:"POST"}); }
      catch (error) { errorBox(card, error.message); }
      return;
    }

    const accept = event.target.closest("[data-encounter-accept]");
    if (accept) {
      const card = accept.closest("[data-encounter-id]");
      accept.disabled = true;
      try {
        await CM.api(`/v1/encounters/${accept.dataset.encounterAccept}/accept`, {method:"POST"});
        await CM.loadCharacters();
        await refresh();
      } catch (error) {
        accept.disabled = false;
        errorBox(card, error.message);
      }
      return;
    }

    const dismiss = event.target.closest("[data-encounter-dismiss]");
    if (dismiss) {
      const card = dismiss.closest("[data-encounter-id]");
      dismiss.disabled = true;
      try {
        await CM.api(`/v1/encounters/${dismiss.dataset.encounterDismiss}/dismiss`, {method:"POST"});
        await refresh();
      } catch (error) {
        dismiss.disabled = false;
        errorBox(card, error.message);
      }
    }
  });

  list.addEventListener("submit", async event => {
    const form = event.target.closest("[data-encounter-form]");
    if (!form) return;
    event.preventDefault();
    const card = form.closest("[data-encounter-id]");
    const input = form.querySelector("input");
    const message = String(input?.value || "").trim();
    if (!message) return;
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      await CM.api(`/v1/encounters/${form.dataset.encounterForm}/messages`, {
        method:"POST",
        body:JSON.stringify({message}),
      });
      input.value = "";
      await refresh();
      const refreshed = list.querySelector(`[data-encounter-id="${form.dataset.encounterForm}"]`);
      refreshed?.querySelector("[data-encounter-panel]")?.classList.remove("hidden");
      refreshed?.scrollIntoView({block:"nearest"});
    } catch (error) {
      errorBox(card, error.message);
    } finally {
      button.disabled = false;
    }
  });

  CM.registerFeature("encounter", {refresh});
})();
