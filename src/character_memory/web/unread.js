(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before unread.js");

  const unreadBootstrapKey = "character-memory:unread-v1-initialized";
  const summaryPollMs = 10000;
  const summaries = new Map();
  let busy = false;

  const lastReadKey = id => `character-memory:last-read:${id}`;
  const lastReadId = id => Number(localStorage.getItem(lastReadKey(id)) || 0);
  const latestAssistantId = id => Number(summaries.get(id)?.latest_assistant_message_id || 0);

  function markRead(id) {
    const latest = latestAssistantId(id);
    if (latest > 0) localStorage.setItem(lastReadKey(id), String(latest));
  }

  function isUnread(id) {
    if (!CM.isGroupConversation() && id === CM.state.characterId) return false;
    const latest = latestAssistantId(id);
    return latest > 0 && latest > lastReadId(id);
  }

  function preview(profile) {
    const latest = summaries.get(profile.id)?.latest_message;
    if (!latest) return profile.tagline || profile.identity || "Persistent AI Person";
    const mediaFallback = latest.image
      ? `[图片] ${latest.image.label || "图片"}`
      : (latest.sticker ? `[表情包] ${latest.sticker.label || "表情包"}` : "");
    const body = latest.preview || latest.content || mediaFallback;
    if (!body) return profile.tagline || profile.identity || "Persistent AI Person";
    return `${latest.role === "user" ? "你：" : ""}${body}`;
  }

  function bootstrapBaseline() {
    if (localStorage.getItem(unreadBootstrapKey)) return;
    if (!CM.state.characters.length || !CM.state.characters.every(profile => summaries.has(profile.id))) return;
    for (const profile of CM.state.characters) {
      const latest = latestAssistantId(profile.id);
      if (latest > 0) localStorage.setItem(lastReadKey(profile.id), String(latest));
    }
    localStorage.setItem(unreadBootstrapKey, "1");
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const current = CM.state.characterId;
      const before = latestAssistantId(current);
      const data = await CM.api("/v1/characters/summaries");
      for (const item of data.characters || []) summaries.set(item.id, item);
      bootstrapBaseline();

      const currentLatest = latestAssistantId(current);
      const currentUnread = !CM.isGroupConversation() && currentLatest > 0 && currentLatest > lastReadId(current);
      if (currentUnread && !CM.dom.drawer.classList.contains("open")) {
        await CM.loadDirectHistory();
      } else {
        CM.renderCharacterList();
      }
      if (currentLatest > before) console.info("[unread] current character received new message", current, currentLatest);
    } catch (error) {
      console.warn("character summary refresh failed", error);
    } finally {
      busy = false;
    }
  }

  const feature = CM.registerFeature("unread", {markRead, isUnread, preview, refresh});
  CM.on("ready", async () => {
    await refresh();
    setInterval(() => { if (!document.hidden) refresh(); }, summaryPollMs);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  });
})();
