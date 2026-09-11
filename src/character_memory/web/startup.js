(() => {
  const gate = () => document.getElementById("startupGate");
  const status = () => document.getElementById("startupStatus");
  const detail = () => document.getElementById("startupDetail");
  const retry = () => document.getElementById("startupRetry");
  const continueButton = () => document.getElementById("startupContinue");

  async function warmup() {
    const root = gate();
    if (!root) return;
    root.classList.remove("startup-error", "startup-ready");
    document.body.classList.add("startup-pending");
    status().textContent = "正在唤醒人物…";
    detail().textContent = "正在加载记忆、人物设定与表情资源";
    retry().classList.add("hidden");
    continueButton().classList.add("hidden");

    const started = performance.now();
    try {
      const response = await fetch("/v1/runtime/warmup", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: "{}",
      });
      let payload = {};
      try { payload = await response.json(); } catch (_) { payload = {}; }
      if (!response.ok) {
        const message = typeof payload?.detail === "string" ? payload.detail : `HTTP ${response.status}`;
        throw new Error(message);
      }

      const elapsed = performance.now() - started;
      const seconds = Math.max(elapsed, Number(payload.warmup_ms || 0)) / 1000;
      status().textContent = "准备完成";
      detail().textContent = `${payload.characters || 0} 位人物 · ${payload.stickers || 0} 张表情 · ${seconds.toFixed(1)}s`;
      root.classList.add("startup-ready");
      await new Promise(resolve => setTimeout(resolve, 180));
      root.classList.add("hidden");
      document.body.classList.remove("startup-pending");
      window.dispatchEvent(new CustomEvent("cm:runtime-ready", {detail: payload}));
    } catch (error) {
      root.classList.add("startup-error");
      status().textContent = "Runtime 没有准备好";
      detail().textContent = error?.message || "初始化失败";
      retry().classList.remove("hidden");
      continueButton().classList.remove("hidden");
      console.error("runtime warmup failed", error);
    }
  }

  window.addEventListener("DOMContentLoaded", () => {
    retry()?.addEventListener("click", () => warmup());
    continueButton()?.addEventListener("click", () => {
      gate()?.classList.add("hidden");
      document.body.classList.remove("startup-pending");
    });
    warmup();
  }, {once: true});
})();
