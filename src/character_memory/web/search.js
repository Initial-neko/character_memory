(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before search.js");

  const currentButton = document.getElementById("conversationSearchButton");
  const globalButton = document.getElementById("globalSearchButton");
  const chatShell = document.querySelector(".chat-shell");
  let mode = "current";
  let query = "";
  let results = [];
  let debounceTimer = null;
  let returnButton = null;

  function contextLabel() {
    if (mode === "global") return "全部单聊与群聊的真实消息记录";
    if (CM.isGroupConversation()) return "当前群聊中的消息";
    return `与 ${CM.currentProfile().name || CM.state.characterId} 的聊天消息`;
  }

  function highlight(text, needle) {
    const source = String(text || "");
    const q = String(needle || "");
    if (!q) return CM.escapeHtml(source);
    const lower = source.toLocaleLowerCase();
    const target = q.toLocaleLowerCase();
    let cursor = 0;
    let html = "";
    while (cursor < source.length) {
      const index = lower.indexOf(target, cursor);
      if (index < 0) { html += CM.escapeHtml(source.slice(cursor)); break; }
      html += CM.escapeHtml(source.slice(cursor, index));
      html += `<mark>${CM.escapeHtml(source.slice(index, index + q.length))}</mark>`;
      cursor = index + q.length;
    }
    return html || CM.escapeHtml(source);
  }

  function renderShell() {
    CM.openDrawer(mode === "global" ? "搜索所有聊天" : "搜索当前聊天", contextLabel());
    CM.dom.drawerBody.innerHTML = `
      <div class="message-search">
        <div class="search-mode-switch">
          <button type="button" data-search-mode="current" class="${mode === "current" ? "active" : ""}">当前聊天</button>
          <button type="button" data-search-mode="global" class="${mode === "global" ? "active" : ""}">全局</button>
        </div>
        <label class="search-input-wrap"><span>🔎</span><input type="search" data-message-search-input maxlength="200" placeholder="搜索聊天内容" value="${CM.escapeHtml(query)}" autocomplete="off"></label>
        <div class="search-result-status" data-message-search-status>${query ? "正在搜索…" : "输入关键词开始搜索"}</div>
        <div class="search-result-list" data-message-search-results></div>
      </div>`;
    const input = CM.dom.drawerBody.querySelector("[data-message-search-input]");
    input?.focus();
    if (query) runSearch(query).catch(console.error);
  }

  function renderResults() {
    const status = CM.dom.drawerBody.querySelector("[data-message-search-status]");
    const list = CM.dom.drawerBody.querySelector("[data-message-search-results]");
    if (!status || !list) return;
    if (!query) {
      status.textContent = "输入关键词开始搜索";
      list.innerHTML = "";
      return;
    }
    status.textContent = results.length ? `${results.length} 个结果` : "没有找到匹配消息";
    list.innerHTML = results.map((item, index) => {
      const conversation = item.scope === "GROUP" ? item.conversation_name : item.conversation_name;
      const scopeLabel = item.scope === "GROUP" ? "群聊" : "单聊";
      return `<button type="button" class="search-result-item" data-search-result="${index}">
        <span class="search-result-head"><strong>${CM.escapeHtml(conversation || "聊天")}</strong><span>${scopeLabel} · ${CM.escapeHtml(CM.fmtDate(item.event_time))} ${CM.escapeHtml(CM.fmtTime(item.event_time))}</span></span>
        <span class="search-result-actor">${CM.escapeHtml(item.actor_name || item.actor_id || "")}</span>
        <span class="search-result-preview">${highlight(item.preview, query)}</span>
      </button>`;
    }).join("");
  }

  async function runSearch(value) {
    query = String(value || "").trim();
    if (!query) { results = []; renderResults(); return; }
    const params = new URLSearchParams({q:query, limit:"50"});
    if (mode === "global") {
      params.set("scope", "global");
    } else if (CM.isGroupConversation()) {
      params.set("scope", "group");
      params.set("conversation_id", CM.state.conversation.groupId);
    } else {
      params.set("scope", "direct");
      params.set("character_id", CM.state.characterId);
    }
    const status = CM.dom.drawerBody.querySelector("[data-message-search-status]");
    if (status) status.textContent = "正在搜索…";
    const requestedQuery = query;
    const data = await CM.api(`/v1/search/messages?${params.toString()}`);
    if (requestedQuery !== query) return;
    results = data.results || [];
    renderResults();
  }

  function scheduleSearch(value) {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => runSearch(value).catch(error => {
      const status = CM.dom.drawerBody.querySelector("[data-message-search-status]");
      if (status) status.textContent = `搜索失败：${error.message}`;
    }), 220);
  }

  function removeReturnButton() {
    returnButton?.remove();
    returnButton = null;
  }

  function showReturnLatest() {
    removeReturnButton();
    returnButton = document.createElement("button");
    returnButton.type = "button";
    returnButton.className = "search-return-latest";
    returnButton.textContent = "返回最新消息";
    returnButton.addEventListener("click", async () => {
      try {
        if (CM.isGroupConversation()) await CM.features.groups?.loadHistory?.();
        else await CM.loadDirectHistory();
        removeReturnButton();
      } catch (error) { console.error("return latest failed", error); }
    });
    chatShell?.appendChild(returnButton);
  }

  function locateMessage(eventId) {
    const row = CM.dom.chat.querySelector(`[data-message-id="${CSS.escape(String(eventId))}"]`);
    if (!row) return false;
    CM.dom.chat.querySelectorAll(".search-hit").forEach(item => item.classList.remove("search-hit"));
    row.classList.add("search-hit");
    row.scrollIntoView({block:"center", behavior:"smooth"});
    setTimeout(() => row.classList.remove("search-hit"), 5000);
    return true;
  }

  async function jumpTo(item) {
    if (!item) return;
    CM.closeDrawer();
    if (item.scope === "GROUP") {
      if (!CM.isGroupConversation() || CM.state.conversation.groupId !== item.conversation_id) {
        await CM.features.groups?.enter?.(item.conversation_id);
      }
      if (item.jump_before_id != null) await CM.features.groups?.loadHistory?.({beforeId:item.jump_before_id});
      else await CM.features.groups?.loadHistory?.();
    } else {
      if (CM.isGroupConversation() || CM.state.characterId !== item.character_id) {
        await CM.switchCharacter(item.character_id);
      }
      if (item.jump_before_id != null) await CM.loadDirectHistory({beforeId:item.jump_before_id});
      else await CM.loadDirectHistory();
    }
    showReturnLatest();
    requestAnimationFrame(() => locateMessage(item.event_id));
    setTimeout(() => locateMessage(item.event_id), 120);
  }

  function open(nextMode) {
    mode = nextMode === "global" ? "global" : "current";
    query = "";
    results = [];
    renderShell();
  }

  currentButton?.addEventListener("click", () => open("current"));
  globalButton?.addEventListener("click", () => open("global"));

  CM.dom.drawerBody.addEventListener("input", event => {
    const input = event.target.closest("[data-message-search-input]");
    if (input) scheduleSearch(input.value);
  });
  CM.dom.drawerBody.addEventListener("click", event => {
    const modeButton = event.target.closest("[data-search-mode]");
    if (modeButton) {
      mode = modeButton.dataset.searchMode;
      results = [];
      renderShell();
      return;
    }
    const resultButton = event.target.closest("[data-search-result]");
    if (resultButton) jumpTo(results[Number(resultButton.dataset.searchResult)]).catch(console.error);
  });

  CM.on("conversationChanged", removeReturnButton);
  CM.registerFeature("search", {open,jumpTo,removeReturnButton});
})();
