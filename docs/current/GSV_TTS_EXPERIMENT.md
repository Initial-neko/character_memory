# GSV-TTS-Lite Experiment

Status: available in both TTS Lab and the formal `:8001/v1/tts` route. V1 still uses one configured reference/voice.

## Architecture

Character Memory keeps GSV-TTS-Lite isolated from the main Python environment:

    Browser -> Media Runtime :8001
      -> :9002 TTS Provider Runtime + Lab
      -> GSV sidecar :9014
      -> external GSV-TTS-Lite Python 3.12 CUDA environment
      -> full WAV response

The browser still knows nothing about the GSV-specific sidecar. It always calls `:8001/v1/tts`; Media Runtime routes `tts_provider=gsv` through `:9002` to `:9014`.

## Local runtime

Expected source/runtime layout:

    .external/GSV-TTS-Lite/
      gsv_tts/
      .venv/

The external directory, CUDA Torch environment, model weights and reference audio are local assets and must not be committed.

Required environment variables:

    GSV_TTS_GPT_MODEL
    GSV_TTS_SOVITS_MODEL
    GSV_TTS_REF_AUDIO
    GSV_TTS_REF_TEXT

Optional environment variables:

    GSV_TTS_ROOT
    GSV_TTS_VENV
    GSV_TTS_HOST
    GSV_TTS_PORT
    GSV_TTS_DEVICE
    GSV_TTS_MODELS_DIR
    GSV_TTS_VOICE
    GSV_TTS_LANGUAGE
    GSV_TTS_PROMPT_LANGUAGE
    GSV_TTS_PRELOAD
    GSV_TTS_SEED

Defaults:

    host = 127.0.0.1
    port = 9014
    device = cuda
    voice = murasame
    language = zh
    prompt_language = auto
    preload = 1 when scripts/start-gsv-tts.sh is used
    seed = 1234

## Start

Use Windows-style forward-slash paths for local model assets when launching from Git Bash, for example:

    export GSV_TTS_GPT_MODEL="C:/path/to/voice.ckpt"
    export GSV_TTS_SOVITS_MODEL="C:/path/to/voice.pth"
    export GSV_TTS_REF_AUDIO="C:/path/to/reference.wav"
    export GSV_TTS_REF_TEXT="reference transcript"
    export GSV_TTS_VOICE="murasame"

    bash scripts/start-gsv-tts.sh

Health:

    http://127.0.0.1:9014/health

Then start the Lab in the normal environment:

    uv run character-tts-lab

Open:

    http://127.0.0.1:9002/tts

## Sidecar contract

Endpoints:

    GET  /health
    POST /v1/load
    POST /v1/tts

TTS request:

    {
      "text": "你好",
      "voice": "murasame",
      "language": "zh",
      "speed": 1.0
    }

Response is audio/wav and exposes the same measurement header family used by the Qwen3 sidecar:

    X-TTS-Provider
    X-TTS-Voice
    X-TTS-Model
    X-TTS-Device
    X-TTS-Inference-Ms
    X-TTS-Audio-Ms
    X-TTS-RTF
    X-TTS-Sample-Rate
    X-TTS-Cuda-Allocated-MB
    X-TTS-Cuda-Reserved-MB
    X-TTS-Cuda-Peak-MB
    X-TTS-Seed

## Determinism

GSV-TTS-Lite draws every random number from PyTorch's **global** RNG and accepts no
`generator=` anywhere — token sampling (Gumbel-max, `GPT_SoVITS/GPT/utils.py:5-9`) and
decoder noise (`GPT_SoVITS/SoVITS/models.py:404`) both. The sidecar therefore calls
`torch.manual_seed()` immediately before `infer_batched`.

**It must run inside the sidecar's `RLock`.** Seeding outside the lock lets another
thread consume the RNG state between the seed and the inference, which silently
restores the non-determinism it was meant to remove. `tests/test_gsv_tts_experiment.py`
pins this ordering, not just the seed's existence.

`GSV_TTS_SEED` (default `1234`) sets the runtime default. `"none"`, `"off"`, `"random"`
and `"-1"` disable seeding and restore the old behaviour. A request may carry its own
`seed`; `null` means "use the runtime default".

Measured on the real HTTP path, same 25-character text, same process, 5× `POST :9014/v1/tts`:

| | duration | waveform |
|---|---|---|
| before | 25–88 % spread | 5/5 distinct |
| after (`seed=1234`) | **6720.0 ms, 5/5** | 4/5 byte-identical |

Run 1 differs from runs 2–5 only by first-inference cuDNN kernel warm-up float noise:
corr 0.999986, SNR 45.7 dB, max abs diff 0.96 % FS — inaudible. Expect one slightly
different waveform after a fresh model load, then exact repeats.

### Seed choice is not a fidelity lever

A two-text sample suggested seed 0 was best (0.7989) and 1234 worst (0.7342). **Five
fresh texts did not reproduce it** — all three seeds landed within 0.007. Pooled over
n=7, via Eres2Net speaker similarity:

| seed | mean | min | max |
|---|---|---|---|
| 0 | 0.7552 | 0.6509 | 0.8306 |
| 99999 | 0.7445 | 0.5796 | 0.8359 |
| 1234 | 0.7333 | 0.6787 | 0.8193 |

Between-seed spread is 0.0218; between-sentence spread is 0.2563 — **~12× larger**. What
is said dominates identity, not the seed. Do not build a "scan seeds, keep the best
score" flow: that sells a measurement that does not replicate. The seed's job is
**reproducibility**. Choose its value by pacing (the same short line ranges 1276–11520 ms
across seeds — some seeds stretch three characters into 11.5 s) and by ear.

`temperature`/`top_k`/`top_p`/`repetition_penalty`/`noise_scale` are deliberately **not**
exposed per request. They are the other end of the same lever as the seed; making them
per-request would hand every caller a knob that destroys the reproducibility just gained.
If timbre ever needs tuning, pin per-voice values in the voice profile instead.

## V1 inference policy

V1 deliberately uses GSV-TTS-Lite infer_batched and returns a complete WAV.

The locally measured whole-sentence latency is already low enough to validate the product path without adding streaming protocol or browser playback complexity. TTFT/infer_stream remains a follow-up experiment only if real chat audition shows a first-sentence problem.

At load time the sidecar:

1. creates the GSV TTS engine;
2. loads the configured GPT model;
3. loads the configured SoVITS model;
4. pre-caches speaker audio;
5. pre-caches prompt audio/text.

scripts/start-gsv-tts.sh enables preload by default so the first Lab request does not pay model load cost.

## Current boundary

Formal GSV routing now has two voice layers:

- `config.py` / Settings select `tts_provider: gsv` and the global/default formal voice;
- Media Runtime routes GSV through `:9002` to the isolated `:9014` sidecar;
- Settings owns the global/default GSV GPT/SoVITS/reference configuration and can hot-configure/reload `:9014`;
- `personas/<character>/voice.yaml` optionally registers a per-character reference;
- Browser voice requests carry the Character id. Registered ids use their character reference; unknown ids degrade to the global/default reference;
- VoiceDesign freeze stores the exact auditioned WAV bytes under `personas/<character>/voice/`, writes provenance + transcript to `voice.yaml`, then calls `POST /v1/voices/reload`.

`GSV_TTS_VOICE` names the global/default reference. It does not replace the per-character registry.

Registry reload deliberately keeps the warm engine resident. A profile may override GPT/SoVITS models; unset profile model fields inherit the global runtime models.

Still out of scope: SSE/WebRTC/token-level browser streaming and a general model-asset management system. Qwen3 VoiceDesign remains optional tooling, not a prerequisite for GSV and not a formal chat Provider.


## Runtime configuration ownership

`config.yaml` does not own GSV model assets. It only selects the formal TTS provider/voice/speed/device.

The five GSV runtime values are persisted in the adjacent project `.env` through Settings Center:

```text
GSV_TTS_GPT_MODEL
GSV_TTS_SOVITS_MODEL
GSV_TTS_REF_AUDIO
GSV_TTS_REF_TEXT
GSV_TTS_VOICE
```

The sidecar can start with these fields missing and report `ready=false`. Settings Center may then configure the running sidecar through `POST /v1/configure`; no full-stack restart is required. Model/reference/device changes unload and reload the GSV engine when needed, while switching away from GSV uses `POST /v1/unload` to release GPU memory.

The project launcher does not inject the whole project `.env` into every child process. Only the GSV child receives its `GSV_TTS_*` values as process environment because that upstream runtime consumes environment variables directly. This preserves the intended precedence `real system env > project .env` and prevents Settings edits from being masked by stale launcher-copied values.
