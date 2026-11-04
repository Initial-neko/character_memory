/**
 * Client-side endpointing for a phone-like call: listen continuously, cut the
 * utterance when the speaker stops, and hand back exactly the speech span.
 * Pure logic on purpose — no AudioContext, no DOM, easy to test.
 */
export class Endpointer {
  #rate;
  #silenceMs;
  #minSpeechMs;
  #maxSpeechMs;
  #calibrationMs;
  #defaultThreshold;
  #floorMin = Infinity;
  #floorCount = 0;
  #calibrationLeft = 0;
  #threshold = 0;
  #speechMs = 0;
  #quietMs = 0;
  #frames = [];
  #preRoll = [];
  #started = false;

  constructor({
    rate = 48000,
    silenceMs = 700,
    minSpeechMs = 250,
    maxSpeechMs = 20000,
    calibrationMs = 400,
    defaultThreshold = 0.006,
  } = {}) {
    this.#rate = rate;
    this.#silenceMs = silenceMs;
    this.#minSpeechMs = minSpeechMs;
    this.#maxSpeechMs = maxSpeechMs;
    this.#calibrationMs = calibrationMs;
    this.#defaultThreshold = defaultThreshold;
    this.#calibrationLeft = calibrationMs;
    this.#threshold = defaultThreshold;
  }

  get threshold() { return this.#threshold; }
  get listening() { return this.#started; }
  get speaking() { return this.#started; }

  reset() {
    this.#speechMs = 0;
    this.#quietMs = 0;
    this.#frames = [];
    this.#preRoll = [];
    this.#started = false;
  }

  #rms(chunk) {
    let sum = 0;
    for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
    return Math.sqrt(sum / chunk.length);
  }

  /** Feed one captured chunk. Returns the utterance samples when it ends, else null. */
  push(chunk) {
    const ms = (chunk.length / this.#rate) * 1000;
    const rms = this.#rms(chunk);

    if (this.#calibrationLeft > 0) {
      this.#floorMin = Math.min(this.#floorMin, rms);
      this.#floorCount += 1;
      this.#calibrationLeft -= ms;
      if (this.#calibrationLeft <= 0) {
        const floor = this.#floorMin === Infinity ? 0 : this.#floorMin;
        this.#threshold = Math.min(0.03, Math.max(this.#defaultThreshold, floor * 2.5));
      }
      this.#preRoll.push(chunk);
      if (this.#preRoll.length > 3) this.#preRoll.shift();
      return null;
    }

    if (rms > this.#threshold) {
      if (!this.#started) {
        this.#started = true;
        this.#frames = [...this.#preRoll];
        this.#preRoll = [];
      }
      this.#speechMs += ms;
      this.#quietMs = 0;
      this.#frames.push(chunk);
    } else if (this.#started) {
      this.#quietMs += ms;
      this.#speechMs += ms * 0.2;
      this.#frames.push(chunk);
      if (this.#quietMs >= this.#silenceMs && this.#speechMs >= this.#minSpeechMs) {
        return this.#finish('silence');
      }
      if (this.#quietMs > this.#silenceMs * 3 && this.#speechMs < this.#minSpeechMs) {
        this.reset();
        return null;
      }
    } else {
      this.#preRoll.push(chunk);
      if (this.#preRoll.length > 3) this.#preRoll.shift();
    }

    if (this.#speechMs >= this.#maxSpeechMs) return this.#finish('length');
    return null;
  }

  #finish(reason) {
    const total = this.#frames.reduce((n, frame) => n + frame.length, 0);
    const samples = new Float32Array(total);
    let offset = 0;
    for (const frame of this.#frames) {
      samples.set(frame, offset);
      offset += frame.length;
    }
    const spokenMs = this.#speechMs;
    this.reset();
    return {samples, rate: this.#rate, spokenMs, reason};
  }
}
