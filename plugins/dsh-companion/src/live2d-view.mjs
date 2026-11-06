/**
 * Reusable Live2D renderer for the companion call stage.
 * Vendor scripts and the model stay in assets/live2d; nothing here is imported
 * from the repository. The preview page remains the reference implementation.
 */
const VENDOR = '/companion/live2d/vendor';
const DEFAULT_MODEL = '/companion/live2d/model/' + encodeURIComponent('大肥鱼.model3.json');

const EXPR_GROUPS = { eyeShape: ['key3', 'key4', 'key5', 'key6'] };
const EXPRS = {
  '脸红': { key7: 1 },
  '爱心眼': { key4: 1 },
  '屑屑眼': { key3: 0, key5: 0, key6: 1, key4: 0 },
  '＞＜': { key3: 0, key4: 0, key5: 1, key6: 0 },
  '圈圈眼': { key3: 1, key4: 0, key5: 0, key6: 0 },
  '流泪': { key2: 1 },
  '兔牙': { key9: 1 },
  '加载': { key8: 1 },
  '黑框眼镜': { key11: 1 },
  '单片眼镜': { key12: 1 },
  '流冷汗': { key13: 1 },
  '黑脸': { key1: 1 },
};

const S = Math.sin;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;

const STATES = {
  idle: {
    label: '空闲',
    pose: (t) => ({
      ParamAngleX: S(t * 0.45) * 4, ParamAngleY: S(t * 0.62 + 0.8) * 2.6, ParamAngleZ: S(t * 0.33 + 0.4) * 3,
      ParamBodyAngleX: S(t * 0.45) * 2.4, ParamBodyAngleZ: S(t * 0.33) * 1.2,
      ParamBreath: 0.42 + 0.28 * S(t * 1.25),
      ParamEyeBallX: S(t * 0.21) * 0.28, ParamEyeBallY: S(t * 0.17 + 1) * 0.14,
      LEar1: S(t * 0.9) * 0.10, REar1: S(t * 0.95 + 1) * 0.10,
    }),
  },
  think: {
    label: '思考中',
    pose: (t) => ({
      ParamAngleX: S(t * 0.5) * 6, ParamAngleY: 6 + S(t * 0.7) * 2, ParamAngleZ: -4 + S(t * 0.4) * 2,
      ParamBodyAngleX: S(t * 0.4) * 2, ParamBreath: 0.38 + 0.2 * S(t * 1.1),
      ParamEyeBallX: S(t * 0.3) * 0.22, ParamEyeBallY: 0.46,
      ParamBrowLY: 0.18, ParamBrowRY: 0.18, BrowInnerUp: 0.22,
      ParamMouthForm: -0.18, ParamEyeLOpen: 0.92, ParamEyeROpen: 0.92,
      LEar1: S(t * 1.4) * 0.16, REar1: S(t * 1.4 + 0.6) * 0.16,
    }),
  },
  wait: {
    label: '等她回',
    pose: (t) => ({
      ParamAngleX: S(t * 0.35) * 5, ParamAngleY: 1 + S(t * 0.4) * 1.5, ParamAngleZ: S(t * 0.25) * 4.5,
      ParamBodyAngleX: S(t * 0.35) * 2.6, ParamBreath: 0.44 + 0.26 * S(t * 1.2),
      ParamEyeBallX: -0.12 + S(t * 0.28) * 0.2, ParamEyeBallY: 0.1,
      ParamEyeLOpen: 1.04, ParamEyeROpen: 1.04,
      BrowInnerUp: 0.16, ParamMouthForm: 0.08,
      LEar1: S(t * 1.1) * 0.13, REar1: S(t * 1.1 + 0.5) * 0.13,
    }),
  },
  speak: {
    label: '说话中',
    pose: (t) => ({
      ParamAngleX: S(t * 1.15) * 3.2, ParamAngleY: S(t * 1.5) * 2, ParamAngleZ: S(t * 0.8) * 2,
      ParamBodyAngleX: S(t * 1.15) * 1.4, ParamBreath: 0.46 + 0.16 * S(t * 1.9),
      ParamEyeBallX: S(t * 0.5) * 0.2, ParamEyeBallY: 0.08,
      ParamEyeLOpen: 1, ParamEyeROpen: 1,
      EyeSmile: 0.22, ParamMouthForm: 0.32,
      LEar1: S(t * 1.6) * 0.12, REar1: S(t * 1.6 + 0.7) * 0.12,
    }),
  },
  pat: {
    label: '被摸头',
    pose: (t) => ({
      ParamAngleX: S(t * 2.2) * 3.4, ParamAngleY: 3, ParamAngleZ: 6 + S(t * 1.1) * 1.5,
      ParamBodyAngleX: S(t * 2.2) * 1.6, ParamBreath: 0.5 + 0.24 * S(t * 2.1),
      ParamEyeBallX: S(t * 1.3) * 0.3, ParamEyeBallY: 0.28,
      ParamEyeLOpen: 0.56, ParamEyeROpen: 0.56,
      EyeSmile: 0.85, ParamMouthForm: 0.55, ParamMouthOpenY: 0.08,
      LEar1: S(t * 6.2) * 0.32, REar1: S(t * 6.2 + 0.4) * 0.32,
      LEar2: S(t * 5.1) * 0.2, REar2: S(t * 5.1 + 0.3) * 0.2,
      key7: 1,
    }),
  },
  tired: {
    label: '累了',
    pose: (t) => ({
      ParamAngleX: S(t * 0.3) * 2, ParamAngleY: -6 + S(t * 0.28) * 1.4, ParamAngleZ: -5 + S(t * 0.22) * 1.6,
      ParamBodyAngleX: -2 + S(t * 0.3) * 1, ParamBreath: 0.34 + 0.2 * S(t * 0.8),
      ParamEyeLOpen: 0.4, ParamEyeROpen: 0.4,
      ParamEyeBallY: -0.2, ParamEyeBallX: S(t * 0.2) * 0.12,
      ParamBrowLY: -0.3, ParamBrowRY: -0.3, BrowInnerUp: -0.1,
      ParamMouthForm: -0.25,
      LEar1: -0.25, REar1: -0.25,
    }),
  },
};

function loadScript(src) {
  const seen = globalThis.__dshCompanionScripts ??= new Set();
  if (seen.has(src)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const el = document.createElement('script');
    el.src = src;
    el.async = false;
    el.onload = () => { seen.add(src); resolve(); };
    el.onerror = () => reject(Error(`Live2D 资源加载失败：${src}`));
    document.head.appendChild(el);
  });
}

export async function createLive2dView({ container, modelUrl = DEFAULT_MODEL, onStatus = () => {}, quality = 'high' } = {}) {
  if (!container) throw Error('Live2D container required');
  await loadScript(`${VENDOR}/live2dcubismcore.min.js`);
  await loadScript(`${VENDOR}/pixi.min.js`);
  await loadScript(`${VENDOR}/cubism4.min.js`);
  if (!globalThis.PIXI?.live2d?.Live2DModel) throw Error('Live2D 运行时未就绪');

  const activeExprs = new Set();
  const runtime = {
    ready: false, core: null, idx: new Map(), state: 'idle', t: 0,
    mouth: 0, mouthTarget: 0, externalMouth: false, speaking: false,
    music: false, musicLv: 0.55, gaze: false, zoom: 1.12,
    mouse: { x: 0, y: 0 }, blink: { next: 2.2, phase: 0, v: 1 },
    fps: 0, frames: 0, fpsT: 0,
  };

  const post = (id, value) => {
    const index = runtime.idx.get(id);
    if (index !== undefined) runtime.core.parameters.values[index] = value;
  };

  function applyExpressions() {
    const keys = {};
    for (const name of activeExprs) Object.assign(keys, EXPRS[name] || {});
    for (const key of EXPR_GROUPS.eyeShape) if (!(key in keys)) keys[key] = 0;
    for (let i = 1; i <= 13; i += 1) post(`key${i}`, keys[`key${i}`] ?? 0);
  }

  function blinkUpdate(dt) {
    const b = runtime.blink;
    if (b.phase === 0) {
      b.next -= dt;
      if (b.next <= 0) { b.phase = 1; b.v = 1; }
    } else if (b.phase === 1) {
      b.v -= dt * 9;
      if (b.v <= 0) { b.v = 0; b.phase = 2; }
    } else if (b.phase === 2) {
      b.v += dt * 6;
      if (b.v >= 1) { b.v = 1; b.phase = 0; b.next = 2.4 + Math.random() * 3.4; }
    }
    return b.v;
  }

  function tick(dt) {
    if (!runtime.ready) return;
    runtime.t += dt;
    const t = runtime.t;
    const pose = (STATES[runtime.state] ?? STATES.idle).pose(t);

    if (runtime.music) {
      const lv = runtime.musicLv;
      const beat = Math.exp(-((t * 2) % 1) * 6);
      pose.ParamBodyAngleX += S(t * 6.28) * 3.2 * lv;
      pose.ParamBodyAngleZ = (pose.ParamBodyAngleZ || 0) + S(t * 3.14) * 2 * lv;
      pose.ParamAngleZ += S(t * 3.14 + 0.6) * 2.4 * lv;
      pose.ParamAngleY += beat * 2.6 * lv;
      pose.LEar1 = (pose.LEar1 || 0) + beat * 0.3 * lv;
      pose.REar1 = (pose.REar1 || 0) + beat * 0.3 * lv;
      pose.ParamBreath = (pose.ParamBreath || 0) + beat * 0.12 * lv;
    }

    if (runtime.gaze) {
      pose.ParamEyeBallX = clamp(runtime.mouse.x * 1.1, -1, 1);
      pose.ParamEyeBallY = clamp(-runtime.mouse.y * 0.9, -1, 1);
      pose.ParamAngleX += clamp(runtime.mouse.x * 6, -6, 6);
      pose.ParamAngleY += clamp(-runtime.mouse.y * 5, -5, 5);
    }

    if (runtime.state === 'speak' && !runtime.externalMouth) {
      runtime.mouthTarget = 0.18 + 0.28 * Math.abs(S(t * 13));
    }
    runtime.mouth = lerp(runtime.mouth, runtime.mouthTarget, clamp(dt * 16, 0, 1));
    if (runtime.state === 'speak') pose.ParamMouthOpenY = runtime.mouth;

    for (const id in pose) post(id, pose[id]);

    const bv = blinkUpdate(dt);
    if (bv < 1) {
      post('ParamEyeLOpen', (pose.ParamEyeLOpen ?? 1) * bv);
      post('ParamEyeROpen', (pose.ParamEyeROpen ?? 1) * bv);
    }

    applyExpressions();

    if (runtime.speaking) {
      post('ParamAngleY', (pose.ParamAngleY || 0) + Math.sin(t * 9) * 1.6 * runtime.mouth);
    }

    runtime.frames += 1;
    runtime.fpsT += dt;
    if (runtime.fpsT > 0.5) {
      runtime.fps = Math.round(runtime.frames / runtime.fpsT);
      runtime.frames = 0;
      runtime.fpsT = 0;
    }
  }

  const lowQuality = quality === 'low';
  const app = new PIXI.Application({
    backgroundAlpha: 0,
    antialias: !lowQuality,
    autoDensity: true,
    resolution: lowQuality ? 1 : Math.min(window.devicePixelRatio || 1, 2),
    powerPreference: lowQuality ? 'low-power' : 'high-performance',
    resizeTo: container,
  });
  app.ticker.maxFPS = lowQuality ? 24 : 60;
  container.appendChild(app.view);

  let model;
  try {
    model = await PIXI.live2d.Live2DModel.from(modelUrl, { autoInteract: false });
  } catch (error) {
    app.destroy(true, { children: true, texture: true, baseTexture: true });
    throw Error(`Live2D 模型加载失败：${error.message}`);
  }
  app.stage.addChild(model);

  const internal = model.internalModel;
  const core = internal.coreModel._model || internal.coreModel;
  runtime.core = core;
  const ids = core.parameters.ids;
  for (let i = 0; i < core.parameters.count; i += 1) runtime.idx.set(ids[i], i);
  if (internal.eyeBlink) internal.eyeBlink = null;

  function fit() {
    const W = app.renderer.width / app.renderer.resolution;
    const H = app.renderer.height / app.renderer.resolution;
    if (!W || !H) return;
    const cw = internal.originalWidth;
    const ch = internal.originalHeight;
    const scale = Math.min((W * 0.82 * runtime.zoom) / cw, (H * 0.96 * runtime.zoom) / ch);
    model.scale.set(scale);
    model.x = (W - cw * scale) / 2;
    model.y = H - ch * scale;
  }
  fit();
  window.addEventListener('resize', fit);

  runtime.ready = true;
  onStatus({ kind: 'ready', params: runtime.idx.size });

  let last = performance.now();
  const ticker = () => {
    const now = performance.now();
    const dt = Math.min((now - last) / 1000, 0.1);
    last = now;
    tick(dt);
  };
  app.ticker.add(ticker);

  // Keep her breathing: replay the idle clip whenever the current one ends.
  const idleLoop = () => { try { model.motion('Idle', 0); } catch {} };
  idleLoop();
  runtime.idleTimer = setInterval(idleLoop, 7000);

  let destroyed = false;
  return {
    get ready() { return runtime.ready; },
    runtime,
    setState(name) {
      if (!STATES[name]) return;
      runtime.state = name;
      runtime.speaking = name === 'speak';
      if (name !== 'speak') { runtime.mouthTarget = 0; runtime.externalMouth = false; }
      if (name === 'pat') activeExprs.add('脸红');
      else activeExprs.delete('脸红');
    },
    setMouth(level) {
      runtime.externalMouth = true;
      runtime.mouthTarget = clamp(Number(level) || 0, 0, 1);
    },
    playMotion(group = 'Gesture', index = 0) {
      try { model.motion(group, index); return true; } catch { return false; }
    },
    setMood(mood) {
      const table = {
        happy: {exprs: ['爱心眼'], motion: ['Gesture', 0]},
        shy: {exprs: ['脸红'], motion: ['Gesture', 2]},
        angry: {exprs: ['黑脸'], motion: ['Gesture', 1]},
        surprised: {exprs: ['圈圈眼']},
        thinking: {exprs: ['加载']},
        smug: {exprs: ['屑屑眼']},
        sad: {exprs: ['＞＜', '流泪']},
        playful: {exprs: ['兔牙']},
        awkward: {exprs: ['流冷汗']},
        glasses: {exprs: ['黑框眼镜']},
        monocle: {exprs: ['单片眼镜']},
        neutral: {exprs: []},
      };
      const plan = table[mood] || table.neutral;
      activeExprs.clear();
      for (const name of plan.exprs) activeExprs.add(name);
      if (plan.motion) {
        try { model.motion(plan.motion[0], plan.motion[1]); } catch {}
        clearTimeout(runtime.resetTimer);
        runtime.resetTimer = setTimeout(() => { try { model.motion('Reset', 0); } catch {} }, 2600);
      }
      return true;
    },
    setExpression(name, on = true) {
      if (!EXPRS[name]) return false;
      if (on) activeExprs.add(name); else activeExprs.delete(name);
      return true;
    },
    clearExpressions() { activeExprs.clear(); },
    setGaze(x, y) {
      runtime.gaze = true;
      runtime.mouse.x = clamp(Number(x) || 0, -1, 1);
      runtime.mouse.y = clamp(Number(y) || 0, -1, 1);
    },
    setZoom(value) { runtime.zoom = clamp(Number(value) || 1.12, 0.6, 2); fit(); },
    destroy() {
      clearInterval(runtime.idleTimer);
      clearTimeout(runtime.resetTimer);
      if (destroyed) return;
      destroyed = true;
      runtime.ready = false;
      let canvas = null;
      try { canvas = app.view; } catch {}
      try { window.removeEventListener('resize', fit); } catch {}
      try { app.ticker.remove(ticker); } catch {}
      try { model.destroy({ children: true, texture: true, baseTexture: true }); } catch {}
      try { app.stage?.removeChildren(); } catch {}
      try { app.renderer?.destroy(); } catch {}
      try { app.ticker?.destroy(); } catch {}
      try { if (canvas?.parentNode) canvas.parentNode.removeChild(canvas); } catch {}
    },
  };
}
