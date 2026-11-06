/** Permission/capture ownership only; never sends frames or starts a model itself. */
import {captureLocalScreen, ensureScreenServer} from './local-screen.mjs';

export class ScreenCapture {
  #call; #media; #revision = 0; #stream = null; #ended = null; #video = null; #local = null; #localActive = false; #localUrl = null;
  constructor({call, mediaDevices = globalThis.navigator?.mediaDevices, local = null, localUrl = null}) {
    this.#call = call;
    this.#media = mediaDevices;
    this.#local = local;
    this.#localUrl = localUrl;
  }
  get stream() { return this.#stream; }
  get localMode() { return !!this.#local; }
  token() { return Object.freeze({call: this.#call.token(), screenRevision: this.#revision}); }
  accepts(token) {
    const live = this.#local ? this.#localActive : !!this.#stream;
    return live && token?.screenRevision === this.#revision && this.#call.accepts(token.call) && this.#call.snapshot.screen === 'ON';
  }
  async chooseFromGesture() {
    if (this.#call.snapshot.phase !== 'ACTIVE') throw Error('start call before choosing screen');
    this.stop();
    const revision = ++this.#revision;
    const callToken = this.#call.token();
    if (this.#local) return this.#chooseLocal(revision, callToken);
    // Electron exposes getDisplayMedia but never resolves it; go local there.
    if (/Electron/i.test(globalThis.navigator?.userAgent ?? '')) {
      if (!this.#localUrl) throw Error('host screen picker unavailable');
      this.#local = {url: this.#localUrl};
      return this.#chooseLocal(revision, callToken);
    }
    if (typeof this.#media?.getDisplayMedia !== 'function') {
      if (!this.#localUrl) throw Error('host screen picker unavailable');
      this.#local = {url: this.#localUrl};
      return this.#chooseLocal(revision, callToken);
    }
    // Invoke before any await: browser transient user activation is required.
    let stream;
    try {
      stream = await this.#media.getDisplayMedia({video: true, audio: false});
    } catch (error) {
      if (!this.#localUrl) throw error;
      this.#local = {url: this.#localUrl};
      return this.#chooseLocal(revision, callToken);
    }
    if (revision !== this.#revision || !this.#call.accepts(callToken)) {
      for (const track of stream.getTracks()) track.stop();
      return null;
    }
    if (!stream.getVideoTracks().length) {
      for (const track of stream.getTracks()) track.stop();
      throw Error('screen source has no video');
    }
    this.#stream = stream;
    this.#ended = () => this.stop();
    for (const track of stream.getTracks()) track.addEventListener('ended', this.#ended);
    this.#call.own('screen', () => this.#clear(), callToken);
    this.#call.screenStarted(stream.getVideoTracks()[0].label || '选定画面');
    return stream;
  }
  async #chooseLocal(revision, callToken) {
    await ensureScreenServer(this.#local);
    if (revision !== this.#revision || !this.#call.accepts(callToken)) return null;
    this.#localActive = true;
    this.#call.own('screen', () => this.#clear(), callToken);
    this.#call.screenStarted('整个屏幕（本地截屏）');
    return {local: true, url: this.#local.url};
  }

  #clear() {
    this.#revision++;
    const stream = this.#stream;
    const ended = this.#ended;
    this.#stream = null;
    this.#ended = null;
    this.#localActive = false;
    if (this.#video) { this.#video.pause(); this.#video.srcObject = null; this.#video = null; }
    if (stream) for (const track of stream.getTracks()) { track.removeEventListener('ended', ended); track.stop(); }
  }
  stop() { this.#clear(); this.#call.stopScreen(); }
  async captureFrame({maxEdge = 1280, quality = 0.8, document = globalThis.document} = {}) {
    const token = this.token();
    if (!this.accepts(token)) throw Error('screen sharing is off');
    if (!Number.isInteger(maxEdge) || maxEdge < 256 || maxEdge > 1920 || !Number.isFinite(quality) || quality < 0.3 || quality > 1) {
      throw Error('invalid frame encoding bounds');
    }
    if (this.#local) {
      const frame = await captureLocalScreen({...this.#local, maxEdge, quality});
      if (!this.accepts(token)) throw Error('screen capture was stopped');
      if (!frame.blob || frame.blob.size > 4 * 1024 * 1024) throw Error('invalid/oversized screen frame');
      return {...frame, token};
    }
    const video = this.#video ?? document.createElement('video');
    this.#video = video;
    video.muted = true;
    video.playsInline = true;
    video.srcObject = this.#stream;
    await video.play();
    if (!this.accepts(token)) throw Error('screen capture was stopped');
    const w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) throw Error('screen frame not ready');
    const scale = Math.min(1, maxEdge / Math.max(w, h));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(w * scale));
    canvas.height = Math.max(1, Math.round(h * scale));
    const context = canvas.getContext('2d');
    if (!context) throw Error('frame encoder unavailable');
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
    if (!this.accepts(token)) throw Error('screen capture was stopped');
    if (!blob || blob.size > 4 * 1024 * 1024) throw Error('invalid/oversized screen frame');
    let fingerprint = null;
    try {
      const sample = document.createElement('canvas');
      sample.width = 8; sample.height = 8;
      const sctx = sample.getContext('2d');
      sctx.drawImage(canvas, 0, 0, 8, 8);
      const pixels = sctx.getImageData(0, 0, 8, 8).data;
      fingerprint = Array.from(pixels).filter((_, i) => i % 4 !== 3).reduce((out, value) => out + Math.round(value / 16).toString(16), '');
    } catch {}
    return {blob, width: canvas.width, height: canvas.height, capturedAt: new Date().toISOString(), token, fingerprint};
  }
}
