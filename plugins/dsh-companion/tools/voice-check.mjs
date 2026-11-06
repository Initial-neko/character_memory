/**
 * End-to-end voice check without a human at the microphone:
 * health -> TTS -> feed that audio back into ASR -> sample the mic.
 * Exit code 0 means every stage answered; 1 means at least one failed.
 */
const TTS = process.env.GSV_URL || 'http://127.0.0.1:9014';
const ASR = process.env.ASR_URL || 'http://127.0.0.1:50131';
const MIC = process.env.MIC_URL || 'http://127.0.0.1:50132';
const SENTENCE = '你好，我是小鱼，今天天气不错。';

const results = [];
const record = (stage, ok, detail) => {
  results.push({stage, ok, detail});
  console.log(`${ok ? 'PASS' : 'FAIL'} ${stage}: ${detail}`);
};

async function health(url) {
  const response = await fetch(`${url}/health`, {signal: AbortSignal.timeout(3000)});
  return response.json();
}

function parseWav(buffer) {
  let offset = 12;
  let sampleRate = 0, channels = 1, bits = 16, data = null;
  while (offset + 8 <= buffer.length) {
    const id = buffer.toString('ascii', offset, offset + 4);
    const size = buffer.readUInt32LE(offset + 4);
    const body = offset + 8;
    if (id === 'fmt ') {
      channels = buffer.readUInt16LE(body + 2);
      sampleRate = buffer.readUInt32LE(body + 4);
      bits = buffer.readUInt16LE(body + 14);
    } else if (id === 'data') {
      data = buffer.subarray(body, body + size);
      break;
    }
    offset = body + size + (size % 2);
  }
  if (!data) throw Error('no data chunk');
  const values = new Float32Array(Math.floor(data.length / (bits / 8)));
  for (let i = 0; i < values.length; i += 1) {
    values[i] = bits === 16 ? data.readInt16LE(i * 2) / 32768 : data.readFloatLE(i * 4);
  }
  return {values, sampleRate, channels};
}

function resample(values, from, to) {
  if (from === to) return values;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(values.length / ratio));
  for (let i = 0; i < out.length; i += 1) {
    const position = i * ratio;
    const left = Math.floor(position);
    const right = Math.min(left + 1, values.length - 1);
    const mix = position - left;
    out[i] = values[left] * (1 - mix) + values[right] * mix;
  }
  return out;
}

async function checkMic() {
  const controller = new AbortController();
  const response = await fetch(`${MIC}/stream`, {signal: controller.signal});
  const reader = response.body.getReader();
  let peak = 0, frames = 0;
  const started = Date.now();
  while (Date.now() - started < 1500) {
    const {value, done} = await reader.read();
    if (done) break;
    if (!value || value.length < 4) continue;
    const samples = new Float32Array(value.buffer, value.byteOffset, Math.floor(value.length / 4));
    for (const sample of samples) peak = Math.max(peak, Math.abs(sample));
    frames += 1;
  }
  controller.abort();
  return {peak, frames};
}

const gsv = await health(TTS).catch(() => null);
record('GSV health', gsv?.ready === true, gsv?.ready ? `loaded ${gsv.model}` : String(gsv?.reason ?? 'unreachable'));

const asr = await health(ASR).catch(() => null);
record('ASR health', asr?.ready === true, asr?.ready ? `waiting ${asr.waiting ?? 0}` : String(asr?.error ?? 'unreachable'));

const mic = await health(MIC).catch(() => null);
record('MIC health', mic?.ready === true, mic?.ready ? `device ${mic.device} @${mic.rate}` : String(mic?.error ?? 'unreachable'));

let wav = null;
if (gsv?.ready) {
  const response = await fetch(`${TTS}/v1/tts`, {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({text: SENTENCE, voice: process.env.VOICE || 'momo', language: 'zh'}),
    signal: AbortSignal.timeout(120000),
  }).catch(() => null);
  if (response?.ok) {
    wav = parseWav(Buffer.from(await response.arrayBuffer()));
    record('TTS synth', wav.values.length > 0, `${wav.values.length} samples @${wav.sampleRate}Hz`);
  } else {
    record('TTS synth', false, `HTTP ${response?.status ?? 'error'}`);
  }
}

if (asr?.ready && wav) {
  const pcm16k = resample(wav.values, wav.sampleRate, 16000);
  const bytes = new Uint8Array(pcm16k.length * 4);
  const view = new DataView(bytes.buffer);
  pcm16k.forEach((value, index) => view.setFloat32(index * 4, value, true));
  const response = await fetch(`${ASR}/asr?sessionId=voice-check&final=1`, {
    method: 'POST',
    headers: {'content-type': 'application/octet-stream'},
    body: bytes,
    signal: AbortSignal.timeout(30000),
  }).catch(() => null);
  const data = response ? await response.json().catch(() => ({})) : {};
  record('ASR round-trip', response?.ok === true && typeof data.text === 'string' && data.text.length > 0, response?.ok ? `heard "${data.text}"` : `HTTP ${response?.status ?? 'error'} ${data.error ?? ''}`);
}

if (mic?.ready) {
  const sample = await checkMic().catch((error) => ({error: String(error)}));
  if (sample.error) record('MIC signal', false, sample.error);
  else record('MIC signal', sample.peak > 0.0003 && sample.frames >= 10, `peak ${sample.peak.toFixed(4)} over ${sample.frames} chunks`);
}

const failed = results.filter((item) => !item.ok);
console.log(JSON.stringify({ok: failed.length === 0, failed: failed.map((item) => item.stage)}, null, 2));
process.exit(failed.length === 0 ? 0 : 1);
