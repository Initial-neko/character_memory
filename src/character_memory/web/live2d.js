/* Optional Live2D presentation for the existing voice call; no independent voice session.
 * Runtime files are intentionally NOT distributed in this repository.
 * See docs/current/VOICE_AND_TTS.md for the local Cubism 5 vendor setup.
 */
(() => {
  const CM = window.CM;
  if (!CM) return;

  const dom = {
    stage: document.getElementById("voiceLive2dStage"),
    avatar: document.getElementById("voiceCallAvatar"),
    card: document.querySelector(".voice-call-card"),
    toggle: document.getElementById("voiceLive2dButton"),
    message: document.getElementById("voiceLive2dMessage"),
  };
  let enabled = false;
  let characterId = null;
  let loadedId = null;
  let generation = 0;
  let renderer = null;
  let paused = false;
  const scriptLoads = new Map();

  function setMessage(message = "") {
    if (!dom.message) return;
    dom.message.textContent = message;
    dom.message.classList.toggle("hidden", !message);
  }

  function updateButton() {
    if (!dom.toggle) return;
    dom.toggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    dom.toggle.classList.toggle("active", enabled);
    dom.toggle.textContent = enabled ? "Live2D ✓" : "Live2D";
  }

  function present(ready) {
    dom.card?.classList.toggle("live2d-on", Boolean(ready));
    dom.stage?.classList.toggle("ready", Boolean(ready));
    dom.stage?.classList.toggle("hidden", !enabled);
    if (dom.avatar) dom.avatar.classList.toggle("hidden", Boolean(ready));
  }

  function loadScript(src) {
    if (scriptLoads.has(src)) return scriptLoads.get(src);
    const pending = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.onload = resolve;
      script.onerror = () => reject(new Error("缺少本地 Live2D 运行时资源：" + src));
      document.head.appendChild(script);
    }).catch(error => {
      scriptLoads.delete(src);
      throw error;
    });
    scriptLoads.set(src, pending);
    return pending;
  }

  async function ensureRuntime() {
    if (!window.WebGL2RenderingContext) {
      throw new Error("当前设备缺少 WebGL 2，无法显示 Live2D");
    }
    const base = "/static/vendor/live2d/";
    if (!window.PIXI?.Application) await loadScript(base + "pixi.min.js");
    if (!window.Live2DCubismCore) await loadScript(base + "live2dcubismcore.min.js");
    if (!window.PIXI?.live2d?.Live2DModel) await loadScript(base + "cubism.js");
    const pixi = window.PIXI;
    if (!pixi?.Application || !pixi?.live2d?.Live2DModel) {
      throw new Error("Live2D 运行时初始化失败，请检查本地 SDK 版本");
    }
    return pixi;
  }

  async function createRenderer(url) {
    const pixi = await ensureRuntime();
    if (!dom.stage) throw new Error("缺少 Live2D 容器");
    const {Live2DModel, Live2DPlugin} = pixi.live2d;
    if (Live2DPlugin) pixi.extensions.add(Live2DPlugin);
    const app = new pixi.Application();
    let model = null;
    try {
      await app.init({
        resizeTo: dom.stage,
        backgroundAlpha: 0,
        preference: "webgl",
        preferWebGLVersion: 2,
        antialias: true,
        autoDensity: true,
        resolution: Math.min(window.devicePixelRatio || 1, 2),
      });
      dom.stage.appendChild(app.canvas);
      model = await Live2DModel.from(url);
      model.anchor?.set(0.5, 0.5);
      app.stage.addChild(model);

      const fit = () => {
        const w = dom.stage.clientWidth;
        const h = dom.stage.clientHeight;
        if (w <= 0 || h <= 0) return;
        app.renderer.resize(w, h);
        const modelWidth = Math.max(1, model.width / (model.scale.x || 1));
        const modelHeight = Math.max(1, model.height / (model.scale.y || 1));
        const scale = Math.min(w * 0.92 / modelWidth, h * 0.94 / modelHeight);
        model.scale.set(scale);
        model.position.set(w / 2, h / 2);
      };
      const observer = new ResizeObserver(fit);
      observer.observe(dom.stage);
      fit();
      try { model.motion?.("Idle", 0); } catch (_) {}
      return {
        pause() { app.ticker?.stop(); },
        resume() { fit(); app.ticker?.start(); },
        destroy() {
          observer.disconnect();
          app.destroy(true, {children: true});
          if (app.canvas?.parentNode) app.canvas.remove();
        },
      };
    } catch (error) {
      try { app.destroy(true, {children: true}); } catch (_) {}
      try { app.canvas?.remove(); } catch (_) {}
      throw error;
    }
  }

  function release() {
    const previous = renderer;
    renderer = null;
    loadedId = null;
    if (previous) {
      try { previous.destroy(); } catch (error) {
        console.warn("[live2d] renderer disposal failed", error);
      }
    }
  }

  async function mount(id) {
    const request = ++generation;
    release();
    present(false);
    if (!enabled || !id || !dom.stage) return;
    dom.stage.classList.remove("hidden");
    setMessage("正在加载 Live2D…");
    let candidate = null;
    try {
      const response = await fetch("/v1/characters/" + encodeURIComponent(id) + "/live2d");
      if (!response.ok) throw new Error("无法读取角色 Live2D 配置（" + response.status + "）");
      const data = await response.json();
      if (!data.available || !data.model_url) {
        throw new Error("当前角色尚未配置 Live2D 模型");
      }
      if (request !== generation || !enabled) return;
      candidate = await createRenderer(data.model_url);
      if (request !== generation || !enabled || id !== characterId) {
        candidate.destroy();
        return;
      }
      renderer = candidate;
      loadedId = id;
      present(true);
      setMessage("");
      if (paused) renderer.pause();
    } catch (error) {
      if (candidate && renderer !== candidate) {
        try { candidate.destroy(); } catch (_) {}
      }
      if (request !== generation || !enabled) return;
      console.warn("[live2d] model unavailable", error);
      enabled = false;
      release();
      present(false);
      setMessage(error.message || "Live2D 模型加载失败，已恢复头像");
      updateButton();
    }
  }

  function setCharacter(id) {
    const next = id || null;
    if (next === characterId && (loadedId === next || !enabled)) return;
    characterId = next;
    if (enabled) mount(next);
  }

  function toggle() {
    enabled = !enabled;
    generation++;
    setMessage("");
    updateButton();
    if (enabled) {
      mount(characterId);
    } else {
      release();
      present(false);
    }
  }

  function pause() {
    paused = true;
    renderer?.pause();
  }

  function resume() {
    paused = false;
    renderer?.resume();
  }

  function stop() {
    generation++;
    enabled = false;
    characterId = null;
    paused = false;
    release();
    present(false);
    setMessage("");
    updateButton();
  }

  dom.toggle?.addEventListener("click", toggle);
  updateButton();
  present(false);
  CM.live2d = {setCharacter, pause, resume, stop, toggle};
})();
