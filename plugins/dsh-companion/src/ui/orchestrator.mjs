import {createMicStream, resample16k} from '../audio-capture.mjs';
import {Endpointer} from './endpointer.mjs';
import {createLocalMicStream, ensureLocalMic} from './local-mic.mjs';
import {checkTranscript} from './transcript-gate.mjs';
import {ScreenWatcher} from './screen-watcher.mjs';

const base = '/companion/api';

/** Thin fetch wrapper: same-origin host routes only, errors carry the server reason. */
export function createRequest(fetchImpl = (...args) => globalThis.fetch(...args)) {
  return async function request(route, options = {}) {
    const response = await fetchImpl(base + route, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.error ?? `HTTP ${response.status}`);
    return data;
  };
}

/** Feed the live model real TTS amplitude while a reply is playing. */
function attachMouth(audio) {
  const set = globalThis.__dshCompanionSetMouth;
  if (typeof set !== 'function') return () => {};
  const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!Ctx) return () => {};
  const ctx = new Ctx();
  const source = ctx.createMediaElementSource(audio);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  source.connect(analyser);
  analyser.connect(ctx.destination);
  const data = new Uint8Array(analyser.frequencyBinCount);
  let raf = 0;
  const tick = () => {
    analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (const value of data) { const x = (value - 128) / 128; sum += x * x; }
    set(Math.min(1, Math.sqrt(sum / data.length) * 2.4));
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
  return () => {
    cancelAnimationFrame(raf);
    set(0);
    try { source.disconnect(); } catch {}
    ctx.close().catch(() => {});
  };
}

export const post = (data) => ({
  method: 'POST',
  headers: {'content-type': 'application/json'},
  body: JSON.stringify(data),
});

/**
 * One continuous microphone, endpointing, a recognition queue and a send queue.
 * Speech that lands while the model is still thinking is transcribed and held,
 * then pushed as soon as the previous turn finishes — the call never stops
 * listening just because a reply is in flight.
 */
export class Orchestrator {
  #request;#call;#screen;#bridge;#store;#fetch;
  #clock = null;
  #stream = null;
  #endpointer = null;
  #micAbort = null;
  #watcher = null;
  #lastFrameHash = '';
  #asrQueue = [];
  #textQueue = [];
  #asrBusy = false;
  #sending = false;
  #playing = false;

  constructor({store, request, call, screen, bridge, fetchImpl = globalThis.fetch}) {
    this.#store = store;
    this.#request = request;
    this.#call = call;
    this.#screen = screen;
    this.#bridge = bridge;
    this.#fetch = (...args) => fetchImpl(...args);
  }

  #set(patch) { this.#store.set(patch); }

  #status(text, activity) {
    this.#set(activity === undefined ? {status: text} : {status: text, activity});
  }

  #queued() { this.#set({queued: this.#asrQueue.length + this.#textQueue.length}); }

  #fail(error) {
    this.#playEnded();
    this.#set({activity: 'idle'});
    this.#status(error?.message ?? String(error));
  }

  async loadConfig() {
    const config = await this.#request('/config');
    this.#set({config});
    return config;
  }

  async begin(mode) {
    // Starting is authoritative: leftover CONNECTING/PAUSED/ERROR is reset instead of blocking.
    if (this.#call.snapshot.phase !== 'IDLE') this.#call.end('restart');
    const config = await this.loadConfig();
    const current = this.#bridge.current();
    if (!current.sessionId) throw Error('请先打开一个原生 DSH 会话');
    if (mode === 'voice' && !config.value.asrEndpoint) throw Error('请先配置 ASR；不会自动下载模型');
    const token = this.#call.start({
      sessionId: current.sessionId,
      personaRevision: 'native-session',
      configRevision: config.revision,
    });
    this.#set({panel: 'call', menu: false, mode, elapsed: 0, activity: 'idle', queued: 0, status: '正在检查连接…'});
    try {
      if (mode === 'voice') {
        const gsv = await this.#request('/health', post({capability: 'gsv'}));
        if (!gsv.ready) throw Error(gsv.reason ?? 'GSV 未就绪');
        const asr = await this.#request('/health', post({capability: 'asr'}));
        if (!asr.ready) throw Error(asr.reason ?? 'ASR 未就绪');
      }
      if (!this.#call.accepts(token)) return;
      this.#call.connected();
      this.#replies(token);
      this.#startClock();
      if (mode === 'voice') await this.#startListening();
      else this.#status('选一个窗口给我看，再说要不要读。');
    } catch (error) {
      this.#call.fail(error, token);
      throw error;
    }
  }

  #startClock() {
    this.#stopClock();
    this.#clock = setInterval(() => {
      if (this.#call.snapshot.phase !== 'ACTIVE') { this.#stopClock(); return; }
      this.#set({elapsed: this.#store.get().elapsed + 1});
    }, 1000);
  }

  #stopClock() {
    if (this.#clock) clearInterval(this.#clock);
    this.#clock = null;
  }

  async end() {
    this.#asrQueue = [];
    this.#textQueue = [];
    this.#sending = false;
    this.#playing = false;
    this.#watcher?.stop();
    this.#watcher = null;
    this.#lastFrameHash = '';
    this.#micAbort?.abort();
    this.#micAbort = null;
    this.#stream?.stop();
    this.#stream = null;
    this.#endpointer = null;
    this.#call.end('user');
    this.#stopClock();
    this.#clearPreview();
    this.#set({panel: null, menu: false, activity: 'idle', queued: 0, status: '已结束。录音、播放和采集都停了。'});
  }

  muteInput(muted) {
    this.#call.muteInput(muted);
    this.#stream?.mute(muted);
    this.#endpointer?.reset();
    if (muted) this.#status('麦克风关了。点「开麦」接着说。', 'idle');
    else this.#status('在听你说。');
  }

  #clearPreview() {
    const {preview} = this.#store.get();
    if (preview) URL.revokeObjectURL(preview);
    this.#set({preview: null, frame: null});
  }

  async #startListening() {
    if (this.#stream) return;
    const token = this.#call.token();
    const config = this.#store.get().config?.value ?? {};
    const abort = new AbortController();
    this.#micAbort = abort;
    this.#set({status: '在打开麦克风…', activity: 'thinking'});
    let stream;
    try {
      if ((config.audioSource ?? 'local') === 'local') {
        const ensured = await ensureLocalMic(this.#request, config.localMicUrl);
        if (!ensured?.ready) throw Error(`本地麦克风服务没起来：${ensured?.error ?? '未知原因'}`);
        stream = await createLocalMicStream({
          url: config.localMicUrl,
          signal: abort.signal,
          onFrame: (chunk) => this.#onFrame(chunk),
        });
      } else {
        stream = await createMicStream({
          deviceId: config.inputDeviceId ?? '',
          signal: abort.signal,
          onFrame: (chunk) => this.#onFrame(chunk),
        });
      }
    } catch (error) {
      if (!this.#call.accepts(token)) return;
      this.#fail(error);
      return;
    }
    if (!this.#call.accepts(token)) { stream.stop(); return; }
    this.#stream = stream;
    const tuning = this.#store.get().config?.value ?? {};
    this.#endpointer = new Endpointer({
      rate: stream.sampleRate,
      silenceMs: tuning.endpointSilenceMs,
      minSpeechMs: tuning.endpointMinSpeechMs,
      maxSpeechMs: tuning.endpointMaxSpeechMs,
      calibrationMs: tuning.endpointCalibrationMs,
      defaultThreshold: tuning.endpointThreshold,
    });
    this.#call.own('microphone', () => {
      stream.stop();
      this.#stream = null;
      this.#endpointer = null;
    }, token);
    this.#set({activity: 'listening', status: '在听你说。说完我就自己接上。'});
  }

  #onFrame(chunk) {
    if (!this.#call.accepts(this.#call.token())) return;
    // Only the reply playback pauses listening; thinking does not, so the next
    // sentence is already transcribed and queued while the model works.
    if (this.#playing || this.#call.snapshot.inputMuted || !this.#endpointer) {
      this.#endpointer?.reset();
      return;
    }
    const done = this.#endpointer.push(chunk);
    if (done) {
      this.#asrQueue.push(done);
      this.#queued();
      this.#pumpAsr();
    }
  }

  async #pumpAsr() {
    if (this.#asrBusy || !this.#asrQueue.length) return;
    this.#asrBusy = true;
    const utterance = this.#asrQueue.shift();
    this.#queued();
    const token = this.#call.token();
    try {
      const text = await this.#transcribe(utterance, token);
      if (text && this.#call.accepts(token)) {
        this.#textQueue.push(text);
        this.#queued();
        this.#pumpSend();
      }
    } catch (error) {
      if (this.#call.accepts(token)) this.#fail(error);
    } finally {
      this.#asrBusy = false;
      this.#queued();
      if (this.#asrQueue.length) this.#pumpAsr();
    }
  }

  async #transcribe({samples, rate, spokenMs}, token) {
    const pcm = resample16k(samples, rate);
    this.#status(`听到 ${(spokenMs / 1000).toFixed(1)} 秒，在转写…`, 'thinking');
    const abort = new AbortController();
    this.#call.own('asr-request', () => abort.abort(), token);
    const url = `/asr?sessionId=${encodeURIComponent(token.sessionId)}&epoch=${token.generation}&final=1`;
    const response = await this.#fetch(base + url, {
      method: 'POST',
      headers: {'content-type': 'application/octet-stream'},
      body: pcm.buffer,
      signal: abort.signal,
    });
    const out = await response.json().catch(() => ({}));
    if (!this.#call.accepts(token)) return null;
    if (!response.ok) throw Error(out.error ?? `ASR HTTP ${response.status}`);
    if (!out.ready) { this.#status('ASR 还在加载，等它一下。'); return null; }
    const text = (out.text ?? '').trim();
    if (!text) { this.#status('没听清，接着说。'); return null; }
    const tuning = this.#store.get().config?.value ?? {};
    const verdict = checkTranscript(text, {
      requireChinese: tuning.requireChinese,
      minChars: tuning.minTranscriptChars,
      chineseRatio: tuning.transcriptChineseRatio,
    });
    if (!verdict.ok) {
      this.#status(`这句像杂音，没发（${verdict.reason}）：${text}`);
      return null;
    }
    return text;
  }

  async #pumpSend() {
    if (this.#sending || !this.#textQueue.length) return;
    const token = this.#call.token();
    const text = this.#textQueue.shift();
    this.#sending = true;
    this.#queued();
    try {
      const cfg = this.#store.get().config?.value ?? {};
      const files = [];
      if (cfg.visionEnabled && this.#call.snapshot.screen === 'ON') {
        let frame = this.#store.get().frame;
        if (!frame || !this.#screen.accepts(frame.token)) {
          try { frame = await this.#screen.captureFrame({maxEdge: cfg.screenMaxEdge, quality: cfg.screenQuality}); } catch { frame = null; }
        }
        if (frame?.blob) files.push(new File([frame.blob], 'screen.png', {type: 'image/png'}));
      }
      this.#bridge.applyTranscript(this.#bridge.current(), text);
      await this.#bridge.submit({token: this.#bridge.current(), text, files});
      this.#reactCommand(text);
      this.#set({messages: [...this.#store.get().messages, {role: 'user', text, at: Date.now()}]});
      if (files.length) this.#set({frame: null});
      this.#status('发出去等回复了，你接着说。', 'thinking');
    } catch (error) {
      this.#sending = false;
      this.#fail(error);
    }
  }

  #playStarted() {
    this.#playing = true;
    this.#stream?.mute(true);
    this.#endpointer?.reset();
    this.#set({activity: 'speaking', status: '在说…'});
  }

  #playEnded() {
    this.#playing = false;
    this.#stream?.mute(this.#call.snapshot.inputMuted);
    this.#endpointer?.reset();
  }

  async #finishTurn() {
    this.#sending = false;
    this.#playEnded();
    this.#pumpSend();
    if (!this.#sending && !this.#textQueue.length && !this.#asrQueue.length) {
      this.#status('我在听，接着说。', this.#call.snapshot.inputMuted ? 'idle' : 'listening');
    }
  }

  /** Turn the reply text into an expression + gesture. Keyword based on purpose. */
  /** User commands that map straight onto a gesture. */
  /** Text typed in the big screen: same path as voice, minus ASR. */
  async sendText(text) {
    const value = String(text ?? '').trim();
    if (!value) return;
    if (value.length > 4000) throw Error('一次最多 4000 字');
    this.#reactCommand(value);
    this.#bridge.applyTranscript(this.#bridge.current(), value);
    const cfg = this.#store.get().config?.value ?? {};
    const files = [];
    if (cfg.visionEnabled && this.#call.snapshot.screen === 'ON') {
      let frame = this.#store.get().frame;
      if (!frame || !this.#screen.accepts(frame.token)) {
        try { frame = await this.#screen.captureFrame({maxEdge: cfg.screenMaxEdge, quality: cfg.screenQuality}); } catch { frame = null; }
      }
      if (frame?.blob) files.push(new File([frame.blob], 'screen.png', {type: 'image/png'}));
    }
    await this.#bridge.submit({token: this.#bridge.current(), text: value, files});
    this.#set({messages: [...this.#store.get().messages, {role: 'user', text: value, at: Date.now()}]});
  }

  #reactCommand(text) {
    const play = globalThis.__dshCompanionPlayMotion;
    if (typeof play !== 'function') return;
    const value = String(text ?? '');
    if (/举手|举个手/.test(value)) play('Gesture', 0);
    else if (/竖手指|指一下/.test(value)) play('Gesture', 1);
    else if (/狗尾草/.test(value)) play('Gesture', 2);
  }

  #reactMood(text) {
    const set = globalThis.__dshCompanionSetMood;
    if (typeof set !== 'function') return;
    const value = String(text ?? '');
    let mood = 'neutral';
    if (/哈哈|开心|喜欢|好耶|嘿嘿|爱你|可爱|欢迎/.test(value)) mood = 'happy';
    else if (/害羞|脸红|讨厌啦|笨蛋|才不|哼/.test(value)) mood = 'shy';
    else if (/生气|坏蛋|不许|不行/.test(value)) mood = 'angry';
    else if (/诶|什么|真的吗|居然|竟然/.test(value)) mood = 'surprised';
    set(mood);
  }

  #replies(token) {
    const events = new EventSource(`${base}/events?sessionId=${encodeURIComponent(token.sessionId)}`);
    let queue = Promise.resolve();
    let audio = null;
    const abort = new AbortController();
    this.#call.own('replies', () => {
      events.close();
      abort.abort();
      if (audio) { audio.pause(); audio.src = ''; }
    }, token);
    events.onmessage = (event) => {
      if (!this.#call.accepts(token)) return;
      let data;
      try { data = JSON.parse(event.data); } catch { return; }
      if (data.sessionId !== token.sessionId) return;
      queue = queue.then(async () => {
        if (!this.#call.accepts(token)) return;
        const text = data.text.replace(/```[\s\S]*?```/g, '').trim();
        this.#reactMood(text);
        if (!text) return;
        this.#playStarted();
        for (let i = 0; i < text.length; i += 380) {
          if (!this.#call.accepts(token)) return;
          const response = await this.#fetch(base + '/tts', {...post({text: text.slice(i, i + 380)}), signal: abort.signal});
          if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw Error(error.error ?? `GSV HTTP ${response.status}`);
          }
          const blob = await response.blob();
          if (!this.#call.accepts(token)) return;
          const url = URL.createObjectURL(blob);
          try {
            audio = new Audio(url);
            const detach = attachMouth(audio);
            audio.addEventListener('ended', detach, {once: true});
            await new Promise((resolve, reject) => {
              audio.onended = resolve;
              audio.onerror = () => reject(Error('音频播放失败'));
              abort.signal.addEventListener('abort', resolve, {once: true});
              audio.play().catch(reject);
            });
          } finally {
            URL.revokeObjectURL(url);
            audio = null;
          }
        }
        if (this.#call.accepts(token)) await this.#finishTurn();
      }).catch((error) => {
        if (!this.#call.accepts(token)) return;
        this.#fail(error);
      });
    };
  }

  async chooseScreen() {
    if (this.#call.snapshot.phase !== 'ACTIVE') throw Error('先开陪伴会话');
    const stream = await this.#screen.chooseFromGesture();
    if (!stream) return;
    this.#clearPreview();
    this.#lastFrameHash = '';
    this.#startWatching();
    this.#status(this.#watcher?.active
      ? `窗口拿到了。她会每 ${Math.round(this.#watcher.state.intervalMs / 1000)} 秒自己看一眼，最多 ${this.#watcher.state.maxReads} 次。`
      : '窗口拿到了。自动看关着，可以手动读一帧。');
  }

  #startWatching() {
    const cfg = this.#store.get().config?.value ?? {};
    if (!cfg.screenAutoRead) return null;
    if (!this.#watcher) {
      this.#watcher = new ScreenWatcher({
        intervalMs: cfg.screenIntervalMs,
        maxReads: cfg.screenMaxReads,
        onTick: () => this.#autoLook(),
      });
    }
    this.#watcher.start();
    this.#set({watch: this.#watcher.state});
    return this.#watcher;
  }

  toggleWatch() {
    if (!this.#watcher) return;
    if (this.#watcher.active) {
      this.#watcher.stop();
      this.#status('自动看暂停了，她不会自己再看。');
    } else if (this.#watcher.state.remaining > 0) {
      this.#watcher.start();
      this.#status(`恢复自动看，每 ${Math.round(this.#watcher.state.intervalMs / 1000)} 秒一次。`);
    } else {
      this.#status('这一轮已经看够了。结束再重开共享才有新的次数。');
    }
    this.#set({watch: this.#watcher.state});
  }

  async #autoLook() {
    if (this.#sending || this.#playing || this.#asrBusy) return;
    const cfg = this.#store.get().config?.value ?? {};
    let frame;
    try {
      frame = await this.#screen.captureFrame({maxEdge: cfg.screenMaxEdge, quality: cfg.screenQuality});
    } catch (error) {
      this.#watcher?.stop();
      this.#set({watch: this.#watcher?.state ?? null});
      this.#status(`自己看的时候失败了，先停：${error.message}`);
      return;
    }
    const n = this.#watcher?.state.reads ?? 0;
    const changed = !(this.#lastFrameHash && frame.fingerprint && frame.fingerprint === this.#lastFrameHash);
    if (changed) this.#lastFrameHash = frame.fingerprint ?? '';
    const {preview} = this.#store.get();
    if (preview) URL.revokeObjectURL(preview);
    this.#set({watch: this.#watcher?.state ?? null, frame, preview: URL.createObjectURL(frame.blob)});
    this.#status(changed ? `第 ${n} 次采样：画面有变化，快照已经更新。` : `第 ${n} 次采样：画面没变，跳过。`);
  }

  async sendFrame() {
    const {frame, config} = this.#store.get();
    if (!config.value.visionEnabled) throw Error('先确认当前模型能看图，再在设置里打开视觉');
    const live = frame && this.#screen.accepts(frame.token);
    const target = live ? frame : await this.#screen.captureFrame({maxEdge: config.value.screenMaxEdge, quality: config.value.screenQuality});
    const token = this.#bridge.current();
    await this.#bridge.submit({
      token,
      text: token.draft || '看看我的屏幕，说说你看到什么。',
      files: [new File([target.blob], 'screen.jpg', {type: 'image/jpeg'})],
    });
    this.#clearPreview();
    this.#status('发给她了。看图的是当前对话模型，回复会走原生聊天。');
  }

  stopScreen() {
    this.#watcher?.stop();
    this.#set({watch: this.#watcher?.state ?? null});
    this.#screen.stop();
    this.#clearPreview();
    this.#status('不共享了。语音还在。');
  }

  async loadVoices() {
    return this.#request('/voices');
  }

  async previewVoice(voice) {
    const response = await this.#fetch(base + '/voice-test', post({voice}));
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw Error(error.error ?? `GSV HTTP ${response.status}`);
    }
    const url = URL.createObjectURL(await response.blob());
    try {
      await new Promise((resolve, reject) => {
        const audio = new Audio(url);
        const detach = attachMouth(audio);
        audio.addEventListener('ended', detach, {once: true});
        audio.onended = resolve;
        audio.onerror = () => reject(Error('试听播放失败'));
        audio.play().catch(reject);
      });
    } finally {
      URL.revokeObjectURL(url);
    }
    this.#status(`声线「${voice}」试听完了。`);
  }

  configure(revision) {
    this.#call.configure(revision);
    this.#set({status: '配置存好了。这通电话要重连才用新配置。'});
  }

  dispose() {
    this.#stopClock();
    this.#micAbort?.abort();
    this.#stream?.stop();
    this.#call.dispose();
  }
}
