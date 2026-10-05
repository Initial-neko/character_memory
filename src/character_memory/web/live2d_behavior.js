/* Presentation coordination; the existing Voice Runtime owns call scheduling. */
(() => {
  const CM = window.CM;
  if (!CM) return;
  CM.createLive2DBehavior = (renderer, capabilities, identity, onChange = () => {}) => {
    let alive = true, paused = false, pending = null, timer = null;
    let lastMotionAt = -Infinity, manualUntil = 0, expressionExpires = 0;
    const seen = new Set();
    let expressionEpoch = 0;
    const state = {automatic: true, phase: "idle", motion: null, expression: null};
    const clearTimer = () => { if (timer !== null) clearTimeout(timer); timer = null; };
    const run = (method, name, key) => {
      const epoch = key === "expression" ? ++expressionEpoch : 0;
      try {
        Promise.resolve(renderer[method](name)).then(result => {
          if (alive && (key !== "expression" || epoch === expressionEpoch) && result !== false) { state[key] = name; onChange({...state}); }
        }).catch(error => console.warn("[live2d] automatic presentation unavailable", error));
      } catch (error) { console.warn("[live2d] automatic presentation unavailable", error); }
    };
    const neutral = () => {
      clearTimer(); expressionExpires = 0;
      if (!alive || paused) return;
      const name = ["普通", "Neutral", "neutral", "Default", "default"].find(item => capabilities.expressions.includes(item));
      if (name) run("setExpression", name, "expression"); else {
        expressionEpoch++;
        try { renderer.resetExpression?.(); state.expression = null; onChange({...state}); }
        catch (error) { console.warn("[live2d] expression reset unavailable", error); }
      }
    };
    const arm = () => {
      clearTimer();
      if (expressionExpires && !paused) timer = setTimeout(neutral, Math.max(0, expressionExpires - Date.now()));
    };
    const apply = hint => {
      if (!alive || paused || Date.now() < manualUntil) return;
      if (capabilities.motions.includes(hint.motion) && Date.now() - lastMotionAt >= 2500) {
        lastMotionAt = Date.now(); run("startMotion", hint.motion, "motion");
      }
      if (capabilities.expressions.includes(hint.expression)) {
        run("setExpression", hint.expression, "expression");
        expressionExpires = Date.now() + 5000; arm();
      }
    };
    onChange({...state});
    return {
      phase(phase) {
        if (!alive) return;
        state.phase = phase; onChange({...state});
        if (paused || Date.now() < manualUntil) return;
        const candidates = phase === "waiting" ? ["Thinking", "Think"] : phase === "recording" ? ["Listening", "Listen"] : [];
        const name = candidates.find(item => capabilities.motions.includes(item));
        if (name && Date.now() - lastMotionAt >= 2500) {
          lastMotionAt = Date.now(); run("startMotion", name, "motion");
        }
      },
      reply(hint, eventId) {
        if (!alive || !hint || hint.token !== identity.token || hint.character_id !== identity.characterId || hint.revision !== capabilities.revision) return false;
        if (eventId != null && seen.has(eventId)) return false;
        if (eventId != null) { seen.add(eventId); if (seen.size > 64) seen.delete(seen.values().next().value); }
        if (paused) pending = {hint, at: Date.now()}; else apply(hint);
        return true;
      },
      manual() {
        expressionEpoch++; manualUntil = Date.now() + 5000;
        clearTimer(); expressionExpires = 0; pending = null;
        try { renderer.cancelPendingExpression?.(); }
        catch (error) { console.warn("[live2d] pending expression cancellation unavailable", error); }
      },
      pause() { paused = true; clearTimer(); },
      resume() {
        paused = false;
        if (expressionExpires && Date.now() >= expressionExpires) neutral(); else arm();
        if (pending && Date.now() - pending.at <= 8000) apply(pending.hint);
        pending = null;
      },
      destroy() { alive = false; pending = null; clearTimer(); seen.clear(); },
    };
  };
})();
