export const DEFAULTS = Object.freeze({
  gsvEndpoint: 'http://127.0.0.1:9014',
  gsvVoice: 'momo',
  gsvSpeed: 1,
  gsvLanguage: 'zh',
  asrEndpoint: '',
  asrProtocol: 'pcm16k-final',
  confirmTranscript: true,
  inputDeviceId: '',
  audioSource: 'local',
  localMicUrl: 'http://127.0.0.1:50132',
  localMicPython: 'python',
  localScreenUrl: 'http://127.0.0.1:50134',
  screenMode: 'manual',
  screenMaxEdge: 1280,
  screenQuality: 0.8,
  screenPreview: true,
  visionEnabled: false,
  autoSend: true,
  endpointSilenceMs: 700,
  endpointMinSpeechMs: 250,
  endpointMaxSpeechMs: 20000,
  endpointThreshold: 0.006,
  endpointCalibrationMs: 400,
  screenAutoRead: false,
  screenIntervalMs: 30000,
  screenMaxReads: 10,
  requireChinese: false,
  minTranscriptChars: 2,
  transcriptChineseRatio: 0.3,
});

/** Fields that older snapshots still carry; read them, keep none of them. */
const LEGACY = new Set(['codexExecutable', 'avatarEnabled', 'avatarUrl', 'avatarScale']);

export const LABELS = Object.freeze({
  gsvEndpoint: 'GSV 服务地址',
  gsvVoice: 'GSV 音色',
  gsvSpeed: '语速',
  gsvLanguage: '合成语言',
  asrEndpoint: 'ASR 服务地址',
  asrProtocol: '识别协议',
  confirmTranscript: '转写确认后发送',
  inputDeviceId: '麦克风设备标识',
  audioSource: '麦克风来源',
  localMicUrl: '本地麦克风服务',
  localMicPython: '启动用的 Python',
  localScreenUrl: '本地截屏服务',
  screenMode: '屏幕读取模式',
  screenMaxEdge: '画面长边上限',
  screenQuality: '画面质量',
  screenPreview: '发送前预览',
  visionEnabled: '已配置 DSH 视觉模型',
  autoSend: '转写后自动发送',
  endpointSilenceMs: '静音多久算说完',
  endpointMinSpeechMs: '最短语音',
  endpointMaxSpeechMs: '最长一句',
  endpointThreshold: '断句灵敏度',
  endpointCalibrationMs: '底噪校准时长',
  screenAutoRead: '自己周期看屏幕',
  screenIntervalMs: '看屏幕间隔',
  screenMaxReads: '每次最多看几次',
  requireChinese: '只发中文识别的结果',
  minTranscriptChars: '转写最短长度',
  transcriptChineseRatio: '中文占比下限',
});

export const HELP = Object.freeze({
  gsvEndpoint: '现有 GSV HTTP 服务地址；保存不会启动服务或生成音频。',
  gsvVoice: 'GSV 现有音色名，须与服务配置一致。',
  gsvSpeed: '只用于之后的合成请求，0.5 到 2。',
  gsvLanguage: 'GSV 合成语言。',
  asrEndpoint: '已配置 ASR 服务地址；不自动下载模型。',
  asrProtocol: 'pcm16k-final 用于本包独立 ASR 服务；旧 dsh-voice-mode 依赖原插件激活，不自动启动。',
  confirmTranscript: '识别结果必须确认后发送，当前固定开启。',
  inputDeviceId: '浏览器麦克风设备；只在浏览器麦克风模式下使用。',
  audioSource: '本地设备服务绕开浏览器权限，桌面端推荐；浏览器麦克风留给网页端。',
  localMicUrl: '本地麦克风服务的 HTTP 地址，默认 50132；开始通话时插件会替你把它拉起来。',
  localMicPython: '启动本地麦克风服务用的解释器，默认 python。',
  localScreenUrl: '桌面端拿不到浏览器屏幕授权时，从这里抓整屏；默认 50134。',
  screenMode: '当前只手动读取选定画面，不持续截屏。',
  screenMaxEdge: '发送前图片长边上限，256 到 1920。',
  screenQuality: 'JPEG 编码质量，0.3 到 1。',
  screenPreview: '发送屏幕图像前必须预览，当前固定开启。',
  visionEnabled: '确认当前 DSH 模型能接收图像后再开；不会自动切换模型。',
  autoSend: '说完自动把转写发出去，像打电话；关掉就只写草稿等你发。',
  endpointSilenceMs: '停顿超过这个毫秒数就认为一句说完，300 到 3000。',
  endpointMinSpeechMs: '太短的杂音不算一句，100 到 2000。',
  endpointMaxSpeechMs: '一口气说太久会被强制切断，2000 到 60000。',
  endpointThreshold: '能量门槛，越小越灵敏也越容易被环境噪声触发，0.001 到 0.1。',
  endpointCalibrationMs: '接通时先听这么久的环境声来定底噪，100 到 2000。',
  screenAutoRead: '共享开始后她自己按间隔看一眼，不用你点。',
  screenIntervalMs: '两次自动看之间隔多少毫秒，10000 到 600000。',
  screenMaxReads: '一轮共享最多自动看几次，看够就停，1 到 100。',
  requireChinese: '杂音常被识别成假名或英文，这种一律不发。',
  minTranscriptChars: '去空白后至少这么多字才算一句话，1 到 50。',
  transcriptChineseRatio: '汉字占非空白字符的最低比例，0.05 到 1。',
});

function endpoint(value, key, empty = false) {
  if (empty && value === '') return '';
  if (typeof value !== 'string' || value.length > 2000) throw Error(`invalid ${key}`);
  let url;
  try { url = new URL(value); } catch { throw Error(`invalid ${key}`); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw Error(`invalid ${key}`);
  return url.href.replace(/\/$/, '');
}

export function validateConfig(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw Error('invalid config');
  for (const key of Object.keys(input)) {
    if (!Object.hasOwn(DEFAULTS, key) && !LEGACY.has(key)) throw Error(`unknown config field ${key}`);
  }
  const c = {...DEFAULTS};
  for (const key of Object.keys(DEFAULTS)) if (Object.hasOwn(input, key)) c[key] = input[key];
  c.gsvEndpoint = endpoint(c.gsvEndpoint, 'gsvEndpoint');
  c.asrEndpoint = endpoint(c.asrEndpoint, 'asrEndpoint', true);
  for (const key of ['gsvVoice', 'gsvLanguage', 'inputDeviceId', 'localMicUrl', 'localMicPython', 'localScreenUrl']) {
    if (typeof c[key] !== 'string' || c[key].length > 2000 || /[\r\n\0]/.test(c[key])) throw Error(`invalid ${key}`);
  }
  if (!c.gsvVoice || c.gsvVoice.length > 100) throw Error('invalid gsvVoice');
  for (const [key, min, max] of [['gsvSpeed', 0.5, 2], ['screenQuality', 0.3, 1], ['screenMaxEdge', 256, 1920]]) {
    if (typeof c[key] !== 'number' || !Number.isFinite(c[key]) || c[key] < min || c[key] > max) throw Error(`invalid ${key}`);
  }
  if (!Number.isInteger(c.screenMaxEdge)) throw Error('invalid screenMaxEdge');
  if (typeof c.visionEnabled !== 'boolean') throw Error('invalid visionEnabled');
  if (typeof c.autoSend !== 'boolean') throw Error('invalid autoSend');
  for (const [key, min, max] of [
    ['endpointSilenceMs', 300, 3000], ['endpointMinSpeechMs', 100, 2000],
    ['endpointMaxSpeechMs', 2000, 60000], ['endpointCalibrationMs', 100, 2000],
  ]) {
    if (!Number.isInteger(c[key]) || c[key] < min || c[key] > max) throw Error(`invalid ${key}`);
  }
  if (typeof c.screenAutoRead !== 'boolean') throw Error('invalid screenAutoRead');
  if (typeof c.requireChinese !== 'boolean') throw Error('invalid requireChinese');
  if (!Number.isInteger(c.minTranscriptChars) || c.minTranscriptChars < 1 || c.minTranscriptChars > 50) throw Error('invalid minTranscriptChars');
  if (typeof c.transcriptChineseRatio !== 'number' || !Number.isFinite(c.transcriptChineseRatio) || c.transcriptChineseRatio < 0.05 || c.transcriptChineseRatio > 1) throw Error('invalid transcriptChineseRatio');
  if (!Number.isInteger(c.screenIntervalMs) || c.screenIntervalMs < 10000 || c.screenIntervalMs > 600000) throw Error('invalid screenIntervalMs');
  if (!Number.isInteger(c.screenMaxReads) || c.screenMaxReads < 1 || c.screenMaxReads > 100) throw Error('invalid screenMaxReads');
  if (!['local', 'browser'].includes(c.audioSource)) throw Error('invalid audioSource');
  c.localMicUrl = endpoint(c.localMicUrl, 'localMicUrl');
  c.localScreenUrl = endpoint(c.localScreenUrl, 'localScreenUrl');
  if (typeof c.endpointThreshold !== 'number' || !Number.isFinite(c.endpointThreshold) || c.endpointThreshold < 0.001 || c.endpointThreshold > 0.1) throw Error('invalid endpointThreshold');
  if (c.confirmTranscript !== true || c.screenPreview !== true || c.screenMode !== 'manual' || !['dsh-voice-mode', 'pcm16k-final'].includes(c.asrProtocol)) {
    throw Error('unsupported automatic mode or provider protocol');
  }
  return Object.freeze(c);
}

export class ConfigStore {
  #snapshot;
  #persist;
  #tail = Promise.resolve();

  constructor({initial = {revision: 0, value: {}}, persist = async () => {}} = {}) {
    if (!Number.isSafeInteger(initial.revision) || initial.revision < 0) throw Error('invalid config revision');
    this.#snapshot = Object.freeze({revision: initial.revision, value: validateConfig(initial.value)});
    this.#persist = persist;
  }

  get snapshot() { return this.#snapshot; }

  update(expectedRevision, patch) {
    const work = this.#tail.then(async () => {
      if (!Number.isSafeInteger(expectedRevision) || expectedRevision !== this.#snapshot.revision) {
        throw Object.assign(Error('configuration conflict; reread before saving'), {status: 409});
      }
      if (!patch || typeof patch !== 'object' || Array.isArray(patch)) throw Error('invalid config patch');
      const value = validateConfig({...this.#snapshot.value, ...patch});
      const next = Object.freeze({revision: this.#snapshot.revision + 1, value});
      await this.#persist(next);
      this.#snapshot = next;
      return next;
    });
    this.#tail = work.catch(() => {});
    return work;
  }
}
