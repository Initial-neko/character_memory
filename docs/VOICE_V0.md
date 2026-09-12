# Voice V0 / Media Runtime

Voice V0 proves one thing: **speech end -> same Character responds -> local TTS starts speaking**.

It intentionally does not solve full-duplex calling, video, voice cloning, emotion TTS, or proactive calls.

## Architecture

Two processes are intentionally independent:

```text
Browser microphone
  -> local VAD / PCM16 WAV
  -> Character Media :8001
       -> ASR (SenseVoiceSmall via sherpa-onnx)
  -> Character Runtime :8000
       -> existing POST /v1/chat/messages
       -> existing PersonRuntime / Memory / Mental State / cloud LLM
       -> existing SSE character_event
  -> Character Media :8001
       -> TTS (small Chinese VITS via sherpa-onnx)
  -> browser playback
```

`character-memory` owns the person. `character-media` owns local media models and is the only process that may own GPU resources.

Stopping/restarting Media Runtime must not stop text chat. Restarting Character Runtime must not require reloading Media Runtime during isolated media development.

## Resource policy

- Main LLM and Vision LLM remain cloud-only.
- ASR + TTS combined GPU budget: **hard target <= 4 GB VRAM**, preferred <= 3 GB.
- CPU is a valid default if warm latency is already inside budget.
- GPU is an accelerator, not a requirement for every media stage.
- Voice V0 does not load a model until the first ASR/TTS request.
- Normal CI never downloads models and never requires a GPU.

## Install

Normal CI / fake-provider tests:

```bash
uv sync --extra api --extra dev
uv run pytest -q
```

CPU media runtime:

```bash
uv sync --extra api --extra media
```

The PyPI `sherpa-onnx` wheel is CPU-oriented. If benchmark data shows GPU is worthwhile, install an official CUDA-enabled sherpa-onnx wheel instead and set the provider to `cuda`. Follow the current sherpa-onnx CUDA wheel instructions rather than adding PyTorch to this project.

## Download the small models

From the repository root in Git Bash:

```bash
mkdir -p models
cd models

curl -L -o sensevoice.tar.bz2 \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
tar -xjf sensevoice.tar.bz2

curl -L -o vits-zh-ll.tar.bz2 \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-vits-zh-ll.tar.bz2
tar -xjf vits-zh-ll.tar.bz2
```

Expected ASR files include `model.int8.onnx` and `tokens.txt`.
Expected TTS files include `model.onnx`, `tokens.txt`, `lexicon.txt`, `dict/`, and rule FSTs.

## Configure Media Runtime

Example CPU baseline in Git Bash:

```bash
export CHARACTER_MEDIA_ASR_MODEL="$PWD/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx"
export CHARACTER_MEDIA_ASR_TOKENS="$PWD/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/tokens.txt"
export CHARACTER_MEDIA_ASR_DEVICE=cpu
export CHARACTER_MEDIA_ASR_THREADS=2
export CHARACTER_MEDIA_ASR_LANGUAGE=zh

export CHARACTER_MEDIA_TTS_MODEL="$PWD/models/sherpa-onnx-vits-zh-ll/model.onnx"
export CHARACTER_MEDIA_TTS_TOKENS="$PWD/models/sherpa-onnx-vits-zh-ll/tokens.txt"
export CHARACTER_MEDIA_TTS_LEXICON="$PWD/models/sherpa-onnx-vits-zh-ll/lexicon.txt"
export CHARACTER_MEDIA_TTS_DICT_DIR="$PWD/models/sherpa-onnx-vits-zh-ll/dict"
export CHARACTER_MEDIA_TTS_RULE_FSTS="$PWD/models/sherpa-onnx-vits-zh-ll/phone.fst,$PWD/models/sherpa-onnx-vits-zh-ll/date.fst"
export CHARACTER_MEDIA_TTS_DEVICE=cpu
export CHARACTER_MEDIA_TTS_THREADS=2
```

Then start the two processes independently:

```bash
# terminal 1
uv run character-memory web --port 8000

# terminal 2
uv run character-media
```

Open `http://127.0.0.1:8000`, select a direct Character, and click the phone button.

The browser uses a simple energy VAD in V0. While the Character is speaking or the cloud model is responding, microphone turn submission is paused. This is deliberately half-duplex.

## HTTP contracts

Media Runtime exposes:

```text
GET  /health
POST /v1/asr              Content-Type: audio/wav; PCM16 mono/stereo
POST /v1/tts              JSON {text, speaker_id, speed}; returns audio/wav
GET  /v1/metrics/recent
```

It does not import or instantiate `PersonRuntime`.

Character Runtime is unchanged. Voice posts recognized text to the existing:

```text
POST /v1/chat/messages
GET  /v1/events/stream
```

Therefore voice is a channel of the same persistent person, not a second voice agent.

## Test strategy

### Layer 1 - deterministic unit/contract tests

Always runs in normal CI, with no sherpa model and no GPU:

- PCM16 WAV parsing and rejection of unsupported WAV formats.
- Resampling contract.
- Fake ASR/TTS provider contracts.
- `/health`, `/v1/asr`, `/v1/tts`, `/v1/metrics/recent` HTTP behavior.
- Bounded latency metric buffer.
- Media process does not import Character Runtime.
- Main server does not import Media Runtime.
- Model dependencies remain lazy.
- Voice frontend uses existing async chat + SSE, remains direct-chat-only, and does not add WebRTC/full-duplex semantics.

These tests answer: **did architecture or behavior break?**

### Layer 2 - real-model local benchmark

This is intentionally not CI. It answers: **where is latency/resources actually spent on this machine?**

Use a short PCM16 WAV sample:

```bash
uv run python scripts/benchmark_media.py \
  --wav path/to/test.wav \
  --iterations 20
```

The script performs one warm-up and reports warm-path mean/p50/p95/max for:

- ASR HTTP total latency
- ASR model inference latency
- TTS HTTP total latency
- TTS model inference latency
- Media-process VRAM through `nvidia-smi` when available

Run the matrix separately:

```text
A: ASR cpu  / TTS cpu
B: ASR cuda / TTS cpu
C: ASR cuda / TTS cuda
```

Do not choose GPU from intuition. Choose the configuration that materially improves end-to-end latency while staying within the combined 4 GB VRAM budget.

### Layer 3 - manual Voice turn benchmark

The call overlay displays a first-pass per-turn breakdown:

```text
ASR ms · LLM wait ms · TTS ms · total ms
```

This is a developer signal, not a permanent product UI. Use it to determine the next optimization target.

## Performance gates for V0

Initial targets, to be revised from real measurements:

```text
ASR warm typical short utterance: < 250 ms
TTS request / first available WAV: < 250 ms
Local ASR+TTS overhead: < 500 ms
ASR+TTS VRAM combined: <= 4 GB
Preferred media VRAM: <= 3 GB
speech-end -> audible character response: <= 1.5 s target, <= 1.0 s ideal
```

If local media is already <500 ms but cloud LLM wait dominates, stop optimizing ASR/TTS and optimize the cloud reaction path instead.

## Deliberate V0 non-goals

- Video/camera input
- Full duplex
- Barge-in while TTS is speaking
- Custom echo cancellation beyond browser audio constraints
- Streaming ASR partial transcripts
- Streaming TTS chunks
- Voice cloning
- Emotion TTS
- Group calls
- Character-initiated calls
- Persistent raw audio

Camera/keyframe work should be a separate P0.19 change after the Voice V0 latency baseline is measurable.
