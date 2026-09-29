class CharacterMemoryAsrCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const requested = Number(options?.processorOptions?.targetSampleRate || 16000);
    this.targetSampleRate = Math.max(8000, Math.min(48000, requested));
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
  }

  process(inputs, outputs) {
    const input = inputs?.[0]?.[0];
    const output = outputs?.[0]?.[0];
    if (output) output.fill(0);
    if (!input || !input.length) return true;

    const values = [];
    for (let i = 0; i < input.length; i += 1) {
      const sample = Math.max(-1, Math.min(1, Number(input[i]) || 0));
      this.sum += sample;
      this.count += 1;
      this.phase += this.targetSampleRate;
      if (this.phase < sampleRate) continue;

      this.phase -= sampleRate;
      const averaged = this.count ? this.sum / this.count : sample;
      values.push(averaged < 0 ? Math.round(averaged * 32768) : Math.round(averaged * 32767));
      this.sum = 0;
      this.count = 0;
    }

    if (values.length) {
      const pcm = Int16Array.from(values);
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor("character-memory-asr-capture", CharacterMemoryAsrCaptureProcessor);
