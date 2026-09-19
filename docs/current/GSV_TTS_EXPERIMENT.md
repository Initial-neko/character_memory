# GSV-TTS-Lite Experiment

Status: Lab-only experiment. It is not yet a formal chat TTS provider.

## Architecture

Character Memory keeps GSV-TTS-Lite isolated from the main Python environment:

    :9002 TTS Provider Runtime + Lab
      -> GSV sidecar :9014
      -> external GSV-TTS-Lite Python 3.12 CUDA environment
      -> full WAV response

The browser still knows nothing about GSV. Formal chat continues to use the existing :8001 Media Runtime route.

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

Defaults:

    host = 127.0.0.1
    port = 9014
    device = cuda
    voice = gsv-default
    language = zh
    prompt_language = auto
    preload = 1 when scripts/start-gsv-tts.sh is used

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

This phase does not:

- add gsv to config.py tts_provider;
- modify Media Runtime formal routing;
- modify Settings Center;
- modify browser voice.js;
- implement per-character voice profiles;
- implement SSE, WebRTC or chunked streaming;
- copy or modify external model assets.

Promotion to a formal provider should only happen after local Lab audition confirms latency, stability, VRAM behavior and voice quality.
