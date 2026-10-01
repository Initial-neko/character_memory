(() => {
  const CM = window.CM;
  if (!CM) return;

  const MEDIA_BASE_KEY = "character-memory:media-base-url";
  const DEFAULT_AUDIO_CONSTRAINTS = {
    echoCancellation:true,
    noiseSuppression:true,
    autoGainControl:true,
  };

  function mediaBase() {
    const resolved = CM.mobileAccess?.mediaBase?.();
    if (resolved) return String(resolved).replace(/\/+$/, "");
    const explicit = String(localStorage.getItem(MEDIA_BASE_KEY) || "").trim();
    return (explicit || "http://127.0.0.1:8001").replace(/\/+$/, "");
  }

  async function requestMicrophone(constraints = DEFAULT_AUDIO_CONSTRAINTS) {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("当前浏览器不支持麦克风采集");
    }
    return navigator.mediaDevices.getUserMedia({audio:constraints});
  }

  function releaseStream(stream) {
    stream?.getTracks?.().forEach(track => track.stop());
  }

  function createCapture(stream) {
    if (!stream) throw new Error("microphone stream is required");
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
      releaseStream(stream);
      throw new Error("当前浏览器不支持 Web Audio");
    }
    let context = null;
    try {
      context = new AudioContextClass();
      const source = context.createMediaStreamSource(stream);
      return {stream, context, source};
    } catch (error) {
      try {
        const closing = context?.close?.();
        closing?.catch?.(() => {});
      } catch (_) {}
      try { releaseStream(stream); } catch (_) {}
      throw error;
    }
  }

  async function closeCapture({stream = null, context = null, source = null, processor = null} = {}) {
    try { processor?.disconnect?.(); } catch (_) {}
    try { source?.disconnect?.(); } catch (_) {}
    try { releaseStream(stream); } catch (_) {}
    try { await context?.close?.(); } catch (_) {}
  }

  function concatChunks(chunks) {
    const length = chunks.reduce((sum, item) => sum + item.length, 0);
    const result = new Float32Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      result.set(chunk, offset);
      offset += chunk.length;
    }
    return result;
  }

  function downsample(samples, sourceRate, targetRate = 16000) {
    if (sourceRate === targetRate) return samples;
    const ratio = sourceRate / targetRate;
    const outLength = Math.max(1, Math.round(samples.length / ratio));
    const out = new Float32Array(outLength);
    for (let i = 0; i < outLength; i += 1) {
      const pos = i * ratio;
      const left = Math.floor(pos);
      const right = Math.min(samples.length - 1, left + 1);
      const frac = pos - left;
      out[i] = samples[left] * (1 - frac) + samples[right] * frac;
    }
    return out;
  }

  function wavBlob(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const writeString = (offset, value) => {
      for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
    };
    writeString(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(36, "data");
    view.setUint32(40, samples.length * 2, true);
    let offset = 44;
    for (let i = 0; i < samples.length; i += 1) {
      const value = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(offset, value < 0 ? value * 32768 : value * 32767, true);
      offset += 2;
    }
    return new Blob([buffer], {type:"audio/wav"});
  }

  CM.mediaAudio = {
    mediaBase,
    requestMicrophone,
    releaseStream,
    createCapture,
    closeCapture,
    concatChunks,
    downsample,
    wavBlob,
  };
})();