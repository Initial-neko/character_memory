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

Formal GSV routing is intentionally minimal:

- `config.py` and Settings Center accept `tts_provider: gsv`;
- Media Runtime routes GSV through the existing `:9002` provider path;
- `character-stack` starts the isolated `:9014` sidecar when GSV is selected;
- TTS Lab remains the place to audition GSV and other providers;
- the formal route uses one configured GSV voice (default `murasame`).

This phase still does **not** implement per-character voice profiles, a voice registry, Qwen3-to-reference asset generation, SSE/WebRTC/chunked TTS, or model/reference asset management. Those remain optional follow-up work after the provider proves useful in real chat.
