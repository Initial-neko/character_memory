(() => {
  const CM = window.CM;
  if (!CM) return;

  const handled = new Set();
  const handledOrder = [];
  const MAX_HANDLED = 1024;
  let audioContext = null;

  function remember(key) {
    if (!key || handled.has(key)) return false;
    handled.add(key);
    handledOrder.push(key);
    while (handledOrder.length > MAX_HANDLED) {
      handled.delete(handledOrder.shift());
    }
    return true;
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

  function notify({scope, conversationId, message} = {}) {
    if (!message || message.role !== "assistant" || message.id == null) return false;
    const key = `${scope || "unknown"}:${conversationId || "unknown"}:${message.id}`;
    if (!remember(key)) return false;
    if (CM.features.voice?.state?.active) return false;
    playSoftDrop();
    return true;
  }

  document.addEventListener("pointerdown", primeAudio, {once:true, capture:true});
  document.addEventListener("keydown", primeAudio, {once:true, capture:true});

  CM.registerFeature("messageSound", {notify, prime:primeAudio});
})();
