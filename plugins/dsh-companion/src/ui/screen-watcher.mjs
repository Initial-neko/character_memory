/**
 * Periodic screen sampling for the companion: she looks on her own, within an
 * explicit budget, and never twice at an unchanged frame.
 */
export class ScreenWatcher {
  #intervalMs;
  #maxReads;
  #onTick;
  #reads = 0;
  #timer = null;
  #busy = false;

  constructor({intervalMs = 30000, maxReads = 10, onTick = async () => {}} = {}) {
    this.#intervalMs = intervalMs;
    this.#maxReads = maxReads;
    this.#onTick = onTick;
  }

  get active() { return this.#timer !== null; }

  get state() {
    return {
      active: this.active,
      reads: this.#reads,
      maxReads: this.#maxReads,
      intervalMs: this.#intervalMs,
      remaining: Math.max(0, this.#maxReads - this.#reads),
    };
  }

  start() {
    if (this.#timer) return this.state;
    this.#timer = setInterval(() => { this.tick(); }, this.#intervalMs);
    return this.state;
  }

  stop() {
    if (this.#timer) clearInterval(this.#timer);
    this.#timer = null;
    return this.state;
  }

  /** One scheduled look. Returns what happened, so callers and tests can see it. */
  async tick() {
    if (!this.active || this.#busy) return 'skip';
    if (this.#reads >= this.#maxReads) { this.stop(); return 'exhausted'; }
    this.#reads += 1;
    this.#busy = true;
    try {
      await this.#onTick(this.#reads);
      return 'read';
    } finally {
      this.#busy = false;
      if (this.#reads >= this.#maxReads) this.stop();
    }
  }
}
