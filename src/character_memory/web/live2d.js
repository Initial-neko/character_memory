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
    controls: document.getElementById("voiceLive2dControls"),
    motion: document.getElementById("voiceLive2dMotion"),
    expression: document.getElementById("voiceLive2dExpression"),
  };
  let enabled = false;
  let characterId = null;
  let generation = 0;
  let renderer = null;
  let pendingRenderer = null;
  let behavior = null;
  let presentation = null;
  let phase = "idle";
  let tokenSequence = 0;
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
    dom.controls?.classList.toggle("hidden", !ready);
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

  async function createRenderer(url, request) {
    const pixi = await ensureRuntime();
    if (request !== generation || !enabled) throw new Error("模型加载已取消");
    if (!dom.stage) throw new Error("缺少 Live2D 容器");
    const {Live2DModel, Live2DPlugin} = pixi.live2d;
    if (Live2DPlugin) pixi.extensions.add(Live2DPlugin);
    const options = {
        resizeTo: dom.stage,
        backgroundAlpha: 0,
        preference: "webgl",
        preferWebGLVersion: 2,
        antialias: true,
        autoDensity: true,
        resolution: Math.min(window.devicePixelRatio || 1, 2),
    };
    const modern = typeof pixi.Application.prototype.init === "function";
    const app = modern ? new pixi.Application() : new pixi.Application(options);
    let model = null;
    let observer = null;
    let canvas = null;
    let disposed = false;
    let appReleased = false;
    const destroy = () => {
      disposed = true;
      observer?.disconnect();
      observer = null;
      if (!appReleased) {
        try { app.destroy(true, {children: true}); appReleased = true; } catch (_) {}
      }
      canvas?.remove();
    };
    const loading = {destroy};
    pendingRenderer = loading;
    try {
      // Local Purism deployment uses Pixi 6; the official deployment uses Pixi 8.
      if (modern) await app.init(options);
      canvas = app.canvas || app.view;
      if (disposed) { destroy(); throw new Error("模型加载已取消"); }
      dom.stage.appendChild(canvas);
      model = await Live2DModel.from(url, {
        autoInteract: false,
        ...(modern ? {} : {autoUpdate: false}),
      });
      if (disposed) {
        model.destroy?.({children: true});
        throw new Error("模型加载已取消");
      }
      if (!modern) {
        // Share the app's lifecycle; Pixi 0.4 defaults to an independent shared ticker.
        app.ticker.add(() => model.update(app.ticker.deltaMS));
        app.ticker.maxFPS = 30;
      }
      model.anchor?.set(0.5, 0.5);
      app.stage.addChild(model);
      const overrides = new Map();
      const core = model.internalModel.coreModel;
      const parameters = core._model?.parameters;
      const parameterRange = id => {
        const index = parameters?.ids?.indexOf(id) ?? core.getParameterIndex?.(id) ?? -1;
        if (index < 0 || (parameters && index >= parameters.ids.length)) return null;
        const min = parameters?.minimumValues?.[index] ?? core.getParameterMinimumValue?.(index);
        const max = parameters?.maximumValues?.[index] ?? core.getParameterMaximumValue?.(index);
        return Number.isFinite(min) && Number.isFinite(max) ? {min, max} : null;
      };
      // Apply after motion/physics and before Core evaluation, using the existing ticker.
      model.internalModel.on("beforeModelUpdate", () => {
        for (const [id, value] of overrides) core.setParameterValueById(id, value);
      });

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
      observer = new ResizeObserver(fit);
      observer.observe(dom.stage);
      fit();
      try { model.motion?.("Idle", 0)?.catch?.(() => {}); } catch (_) {}
      const cancelPendingExpression = () => {
        const manager = model.internalModel.motionManager?.expressionManager;
        // Pixi display 0.4 does not cancel a pending load when the current
        // expression is selected again. Explicitly invalidate that reservation.
        if (manager && "reserveExpressionIndex" in manager) manager.reserveExpressionIndex = -1;
        return manager;
      };
      return {
        controls() {
          const settings = model.internalModel.settings;
          return {motions: Object.keys(settings?.motions || {}), expressions: (settings?.expressions || []).map(item => item.Name || item.name)};
        },
        startMotion(group, index = 0) { return model.motion(group, index, 3); },
        cancelPendingExpression,
        setExpression(name) { cancelPendingExpression(); return model.expression(name); },
        resetExpression() { cancelPendingExpression()?.resetExpression?.(); },
        setParameter(id, value) {
          const range = parameterRange(id);
          if (!range || !Number.isFinite(value)) return false;
          overrides.set(id, Math.max(range.min, Math.min(range.max, value)));
          return true;
        },
        clearParameter(id) { return overrides.delete(id); },
        pause() { app.ticker?.stop(); },
        resume() { fit(); app.ticker?.start(); },
        destroy,
      };
    } catch (error) {
      destroy();
      throw error;
    } finally {
      if (pendingRenderer === loading) pendingRenderer = null;
    }
  }

  function release() {
    behavior?.destroy();
    behavior = null;
    presentation = null;
    if (dom.stage) delete dom.stage.dataset.live2dBehavior;
    const loading = pendingRenderer;
    pendingRenderer = null;
    loading?.destroy();
    const previous = renderer;
    renderer = null;
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
      candidate = await createRenderer(data.model_url, request);
      if (request !== generation || !enabled || id !== characterId) {
        candidate.destroy();
        return;
      }
      renderer = candidate;
      if (data.capabilities && CM.createLive2DBehavior) {
        const token = window.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${++tokenSequence}`;
        presentation = {token, character_id: id, revision: data.capabilities.revision};
        behavior = CM.createLive2DBehavior(renderer, data.capabilities, {token, characterId: id}, state => {
          if (dom.stage) dom.stage.dataset.live2dBehavior = JSON.stringify({characterId: id, ...state});
        });
        behavior.phase(phase);
        if (paused) behavior.pause();
      }
      const controls = renderer.controls();
      for (const [select, names] of [[dom.motion, controls.motions], [dom.expression, controls.expressions]]) {
        if (!select) continue;
        select.replaceChildren();
        for (const name of ["", ...names]) {
          const option = document.createElement("option");
          option.value = name;
          option.textContent = name || "请选择";
          select.appendChild(option);
        }
        select.disabled = !names.length;
      }
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
    if (next === characterId) return;
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
    behavior?.pause();
    renderer?.pause();
  }

  function resume() {
    paused = false;
    renderer?.resume();
    behavior?.resume();
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
  const interact = (method, select) => {
    select?.addEventListener("change", () => {
      if (!select.value || !renderer) return;
      behavior?.manual();
      Promise.resolve(renderer[method](select.value)).catch(error => {
        console.warn("[live2d] interaction failed", error);
        setMessage("模型交互失败：" + (error.message || "未知错误"));
      });
    });
  };
  interact("startMotion", dom.motion);
  interact("setExpression", dom.expression);
  updateButton();
  present(false);
  CM.live2d = {
    setCharacter, pause, resume, stop, toggle,
    requestContext: id => enabled && renderer && presentation && (!id || id === characterId) ? {...presentation} : null,
    setPhase(next) { phase = next; behavior?.phase(next); },
    onReply(id, hint, eventId) { return id === characterId ? behavior?.reply(hint, eventId) ?? false : false; },
    startMotion(group, index = 0) { behavior?.manual(); return renderer?.startMotion(group, index) ?? Promise.resolve(false); },
    setExpression(name) { behavior?.manual(); return renderer?.setExpression(name) ?? Promise.resolve(false); },
    setParameter: (id, value) => renderer?.setParameter(id, value) ?? false,
    clearParameter: id => renderer?.clearParameter(id) ?? false,
  };
})();
