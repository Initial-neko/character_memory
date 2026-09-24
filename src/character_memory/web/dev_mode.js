(() => {
  const STORAGE_KEY = "character-memory:dev-mode";
  const VALID = new Set(["simple", "detailed"]);

  function storedMode() {
    const value = localStorage.getItem(STORAGE_KEY);
    return VALID.has(value) ? value : "simple";
  }

  function apply(mode, {persist = true} = {}) {
    const next = VALID.has(mode) ? mode : "simple";
    document.body.dataset.devMode = next;
    const button = document.getElementById("devModeToggle");
    const hint = document.getElementById("devModeHint");
    if (button) {
      const detailed = next === "detailed";
      button.textContent = detailed ? "退出 Detailed Dev" : "Detailed Dev";
      button.setAttribute("aria-pressed", String(detailed));
      button.title = detailed
        ? "回到简洁 Dev：只保留日常状态、高频调节和 LLM 用量摘要"
        : "显示 Provider Smoke、媒体/视觉测试、底层参数和原始诊断数据";
    }
    if (hint) {
      hint.textContent = next === "detailed"
        ? "Detailed Dev：已显示低频参数、Provider Smoke 与原始诊断工具。"
        : "简洁模式：只显示日常观察与高频调节；后端默认参数不会堆在这里。";
    }
    if (persist) localStorage.setItem(STORAGE_KEY, next);
  }

  function init() {
    apply(storedMode(), {persist:false});
    document.getElementById("devModeToggle")?.addEventListener("click", () => {
      apply(document.body.dataset.devMode === "detailed" ? "simple" : "detailed");
    });
  }

  window.CMDevMode = {apply, storedMode};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
