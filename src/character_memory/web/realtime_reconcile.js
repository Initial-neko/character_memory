(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before realtime_reconcile.js");

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

  // Keep SQLite history authoritative while the in-process SSE hub remains a
  // low-latency notification path. EventSource fires `open` for both the first
  // connection and native reconnects, so each open closes the history/SSE race
  // without introducing a durable message bus or a second cursor protocol.
  const baseConnect = CM.connectDirectStream;
  CM.connectDirectStream = () => {
    baseConnect();
    const source = CM.state.directStream;
    if (!source || source.__cmReconcileAttached) return;
    source.__cmReconcileAttached = true;
    const characterId = CM.state.characterId;
    source.addEventListener("open", () => {
      if (source !== CM.state.directStream) return;
      reconcileDirectLatest(characterId).catch(error => console.warn("[sse reconcile direct]", error));
    });
  };

  CM.registerFeature("realtimeReconcile", {reconcileDirectLatest});
})();
