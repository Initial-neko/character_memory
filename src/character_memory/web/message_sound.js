(() => {
  const CM = window.CM;
  if (!CM) return;

  const conversations = new Map();
  let activeConversationKey = null;
  let baselineReady = false;
  let audioContext = null;

  function conversationKey() {
    return CM.isGroupConversation()
      ? `group:${CM.state.conversation.groupId || "unknown"}`
      : `direct:${CM.state.characterId || "unknown"}`;
  }

  function stateFor(key) {
    if (!conversations.has(key)) conversations.set(key, {seen:new Set(), maxNumericId:Number.NEGATIVE_INFINITY});
    return conversations.get(key);
  }

  function recordMessageId(state, rawId) {
    const id = String(rawId || "").trim();
    if (!id || state.seen.has(id)) return {fresh:false, newer:false};
    state.seen.add(id);
    const numericId = Number(id);
    if (!Number.isFinite(numericId)) return {fresh:true, newer:true};
    const newer = numericId > state.maxNumericId;
    if (numericId > state.maxNumericId) state.maxNumericId = numericId;
    return {fresh:true, newer};
  }

  function seedCurrentConversation() {
    const key = conversationKey();
    const state = stateFor(key);
    activeConversationKey = key;
    baselineReady = true;
    CM.dom.chat?.querySelectorAll?.(".message-row.assistant[data-message-id]").forEach(row => {
      recordMessageId(state, row.dataset.messageId);
    });
  }

  function ensureAudioContext() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;
    if (!audioContext || audioContext.state === "closed") audioContext = new AudioContextClass();
    return audioContext;
  }

  function renderSoftDrop(context) {
    if (!context || context.state !== "running") return;
    const now = context.currentTime;
    const end = now + 0.095;
    const oscillator = context.createOscillator();
    const gain = context.createGain();

    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(660, now);
    oscillator.frequency.exponentialRampToValueAtTime(520, end);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.028, now + 0.008);
    gain.gain.exponentialRampToValueAtTime(0.0001, end);

    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start(now);
    oscillator.stop(end + 0.01);
  }

  function playSoftDrop() {
    try {
      const context = ensureAudioContext();
      if (!context) return;
      if (context.state === "suspended") {
        context.resume().then(() => renderSoftDrop(context)).catch(() => {});
        return;
      }
      renderSoftDrop(context);
    } catch (_) {
      // Notification audio is best-effort only; chat must never fail because audio is unavailable.
    }
  }

  function primeAudio() {
    try {
      const context = ensureAudioContext();
      if (context?.state === "suspended") context.resume().catch(() => {});
    } catch (_) {}
  }

  function inspectAssistantRow(row) {
    if (!(row instanceof Element) || !row.matches(".message-row.assistant[data-message-id]")) return;
    const key = conversationKey();
    if (!baselineReady || key !== activeConversationKey) return;
    const state = stateFor(key);
    const {fresh, newer} = recordMessageId(state, row.dataset.messageId);
    if (!fresh || !newer) return;
    if (CM.features.voice?.state?.active) return;
    playSoftDrop();
  }

  const observer = new MutationObserver(mutations => {
    if (!baselineReady || conversationKey() !== activeConversationKey) return;
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (!(node instanceof Element)) continue;
        inspectAssistantRow(node);
        node.querySelectorAll?.(".message-row.assistant[data-message-id]").forEach(inspectAssistantRow);
      }
    }
  });

  if (CM.dom.chat) observer.observe(CM.dom.chat, {childList:true, subtree:true});
  CM.on("historyLoaded", seedCurrentConversation);
  CM.on("conversationChanged", seedCurrentConversation);

  document.addEventListener("pointerdown", primeAudio, {once:true, capture:true});
  document.addEventListener("keydown", primeAudio, {once:true, capture:true});

  CM.registerFeature("messageSound", {prime:primeAudio, seed:seedCurrentConversation});
})();
