(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before mentions.js");

  let groups = [];
  let menu = null;
  let activeOptions = [];
  let activeIndex = 0;
  let fragment = null;
  const mentionHints = new Map();
  const composerWrap = document.querySelector(".composer-wrap");

  const currentGroup = () => CM.features.groups?.current?.()
    || groups.find(item => item.id === CM.state.conversation.groupId)
    || null;

  async function refreshGroups() {
    try {
      const data = await CM.api("/v1/groups");
      groups = data.groups || [];
    } catch (error) {
      console.warn("[mentions] group refresh failed", error);
    }
  }

  function closeMenu() {
    menu?.remove();
    menu = null;
    activeOptions = [];
    activeIndex = 0;
    fragment = null;
  }

  function findFragment() {
    if (!CM.isGroupConversation()) return null;
    const input = CM.dom.input;
    const caret = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, caret);
    const match = before.match(/(^|\s)@([^\s@]*)$/u);
    if (!match) return null;
    const at = before.length - match[2].length - 1;
    return {start:at, end:caret, query:match[2] || ""};
  }

  function memberOptions(query = "") {
    const group = currentGroup();
    if (!group) return [];
    const needle = query.trim().toLocaleLowerCase();
    const options = [
      {id:"*", name:"所有人", identity:"点名整个群"},
      ...(group.members || []).map(item => ({id:item.id, name:item.name || item.id, identity:item.identity || item.id})),
    ];
    if (!needle) return options;
    return options.filter(item => `${item.name} ${item.id}`.toLocaleLowerCase().includes(needle));
  }

  function renderMenu() {
    fragment = findFragment();
    if (!fragment) { closeMenu(); return; }
    activeOptions = memberOptions(fragment.query);
    if (!activeOptions.length) { closeMenu(); return; }
    activeIndex = Math.min(activeIndex, activeOptions.length - 1);
    if (!menu) {
      menu = document.createElement("div");
      menu.className = "mention-menu";
      composerWrap?.appendChild(menu);
    }
    menu.innerHTML = activeOptions.map((item, index) => `
      <button class="mention-option ${index === activeIndex ? "active" : ""}" type="button" data-mention-index="${index}">
        <span class="mention-avatar">${CM.escapeHtml(item.id === "*" ? "@" : String(item.name).slice(0,1).toUpperCase())}</span>
        <span><strong>@${CM.escapeHtml(item.name)}</strong><small>${CM.escapeHtml(item.identity || "")}</small></span>
      </button>`).join("");
  }

  function insertMention(option, range = fragment) {
    if (!option) return;
    const input = CM.dom.input;
    const token = option.id === "*" ? "@所有人" : `@${option.name}`;
    if (range) {
      input.setRangeText(`${token} `, range.start, range.end, "end");
    } else {
      const caret = input.selectionStart ?? input.value.length;
      const prefix = caret > 0 && !/\s$/.test(input.value.slice(0, caret)) ? " " : "";
      input.setRangeText(`${prefix}${token} `, caret, input.selectionEnd ?? caret, "end");
    }
    mentionHints.set(token, option.id);
    input.dispatchEvent(new Event("input", {bubbles:true}));
    closeMenu();
    input.focus();
  }

  function tokenOccurrences(text, token) {
    const result = [];
    let start = 0;
    while (true) {
      const index = text.indexOf(token, start);
      if (index < 0) break;
      const end = index + token.length;
      const next = text[end];
      if (end >= text.length || /[\s，。！？,.!?;；:：()（）\[\]【】{}<>《》"'、]/u.test(next)) result.push(index);
      start = index + token.length;
    }
    return result;
  }

  function mentionsForText(text, group) {
    if (!group || !text) return [];
    const occurrences = [];
    for (const index of tokenOccurrences(text, "@所有人")) occurrences.push({index, id:"*", length:4});
    for (const member of group.members || []) {
      const name = String(member.name || member.id);
      const tokens = [...new Set([`@${name}`, `@${member.id}`])];
      for (const token of tokens) {
        const hinted = mentionHints.get(token);
        if (hinted && hinted !== member.id) continue;
        for (const index of tokenOccurrences(text, token)) occurrences.push({index, id:member.id, length:token.length});
      }
    }
    occurrences.sort((a,b) => a.index - b.index || b.length - a.length);
    const result = [];
    for (const item of occurrences) {
      if (item.id === "*") return ["*"];
      if (!result.includes(item.id)) result.push(item.id);
    }
    return result;
  }

  // Keep transport ownership in groups.js. This request transform only enriches
  // group-message JSON with stable Character IDs; backend text parsing remains a
  // fallback for manually typed mentions.
  const baseApi = CM.api;
  CM.api = async (path, options = {}) => {
    const match = String(path).match(/^\/v1\/groups\/([^/]+)\/messages(?:\?|$)/);
    if (!match || String(options.method || "GET").toUpperCase() !== "POST" || !options.body) {
      return baseApi(path, options);
    }
    try {
      const payload = JSON.parse(options.body);
      const groupId = decodeURIComponent(match[1]);
      const featureGroup = CM.features.groups?.current?.();
      const group = featureGroup?.id === groupId
        ? featureGroup
        : (groups.find(item => item.id === groupId) || currentGroup());
      const mentions = mentionsForText(String(payload.message || ""), group);
      const result = await baseApi(path, {...options, body:JSON.stringify({...payload, mentions})});
      mentionHints.clear();
      return result;
    } catch (error) {
      if (error instanceof SyntaxError) return baseApi(path, options);
      throw error;
    }
  };

  CM.dom.input.addEventListener("input", () => {
    if (CM.isGroupConversation()) renderMenu();
    else closeMenu();
  });

  CM.dom.input.addEventListener("keydown", event => {
    if (!menu || !activeOptions.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault(); event.stopImmediatePropagation();
      activeIndex = (activeIndex + 1) % activeOptions.length; renderMenu();
    } else if (event.key === "ArrowUp") {
      event.preventDefault(); event.stopImmediatePropagation();
      activeIndex = (activeIndex - 1 + activeOptions.length) % activeOptions.length; renderMenu();
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault(); event.stopImmediatePropagation();
      insertMention(activeOptions[activeIndex]);
    } else if (event.key === "Escape") {
      event.preventDefault(); event.stopImmediatePropagation(); closeMenu();
    }
  }, true);

  composerWrap?.addEventListener("mousedown", event => {
    const button = event.target.closest("[data-mention-index]");
    if (!button) return;
    event.preventDefault();
    insertMention(activeOptions[Number(button.dataset.mentionIndex)]);
  });

  CM.dom.chat.addEventListener("click", event => {
    if (!CM.isGroupConversation()) return;
    const speaker = event.target.closest(".group-speaker-name");
    if (!speaker) return;
    const name = speaker.textContent.trim();
    const matches = (currentGroup()?.members || []).filter(item => String(item.name || item.id) === name);
    if (matches.length === 1) insertMention({id:matches[0].id, name:matches[0].name || matches[0].id, identity:matches[0].identity || ""}, null);
  });

  CM.on("ready", refreshGroups);
  CM.on("conversationChanged", () => { closeMenu(); mentionHints.clear(); });
  CM.registerFeature("mentions", {refreshGroups,close:closeMenu,mentionsForText});
})();
