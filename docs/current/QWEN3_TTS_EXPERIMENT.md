# Qwen3-TTS isolated runtime

Qwen3-TTS is **not** a formal realtime Character Memory TTS provider. Its Torch/CUDA runtime stays isolated in a dedicated sidecar for experiments and future voice-design tooling.

Current product position: the existing 0.6B CustomVoice / optional Base clone code remains experimental infrastructure. Qwen3-TTS is excluded from Settings, formal `tts_provider`, Media Runtime chat routing and the normal `character-stack`. Qwen3-TTS 1.7B VoiceDesign now has a dedicated Workbench UI + adapter contract, but the heavy local runtime remains external and must be connected separately.

## Scope

- isolated sidecar on 127.0.0.1:9013
- isolated Python 3.12 environment: .venv-qwen3-tts
- default model: Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
- default attention: SDPA
- default dtype: auto; CUDA prefers BF16 when supported and otherwise falls back to FP32; CPU uses FP32
- explicit model load endpoint and lazy first-request loading
- latency, RTF and VRAM benchmark
- optional Base-model voice-clone mode
- current CustomVoice request contract already supports `instruct`, but the TTS Lab UI does not expose a voice-style prompt field yet

It is deliberately **not** wired into formal chat configuration or the normal Character Memory stack.

## Install

From Git Bash:

    bash scripts/setup-qwen3-tts.sh --prefetch

This creates .venv-qwen3-tts, installs the official qwen-tts==0.1.1 package and sidecar dependencies, prints Torch/CUDA/GPU information, and prefetches the default 0.6B CustomVoice checkpoint into models/huggingface.

To prefetch the Base checkpoint instead:

    bash scripts/setup-qwen3-tts.sh --prefetch --model Qwen/Qwen3-TTS-12Hz-0.6B-Base

If the environment reports cuda_available: False, fix Torch/CUDA in the isolated environment before judging latency. Do not install Torch into the main Character Memory environment just for this experiment.

## Start the sidecar

    bash scripts/start-qwen3-tts.sh --preload

Default URL is http://127.0.0.1:9013.

Endpoints:

    GET  /health
    POST /v1/load
    POST /v1/tts

Omit --preload when you want the first /v1/load call to measure cold model loading.

Useful overrides:

    QWEN3_TTS_DEVICE=cuda:0 QWEN3_TTS_DTYPE=auto QWEN3_TTS_ATTN=sdpa bash scripts/start-qwen3-tts.sh

For CUDA GPUs with BF16 support, `auto` resolves to `bfloat16`, matching the upstream Qwen3-TTS inference examples. Explicit FP16 is not the formal default because it can trigger unstable generation on some GPUs.

FlashAttention 2 is deliberately not installed by the setup script. Establish a clean SDPA baseline first.

## Benchmark

In another Git Bash:

    bash scripts/benchmark-qwen3-tts.sh --repeats 10

The benchmark records cold model load time, load-time CUDA peak allocation, short/medium/long Chinese replies, HTTP end-to-end latency, model inference latency, generated audio duration, RTF, P50/P95, request peak VRAM, and resident VRAM after the test.

Artifacts are written to data/qwen3-tts-benchmark/:

    short.wav
    medium.wav
    long.wav
    benchmark.json

The WAV files are kept so latency and subjective voice quality can be reviewed together.

## Built-in Chinese voices

The default 0.6B CustomVoice checkpoint includes Vivian, Serena, Uncle_Fu, Dylan and Eric.

Example:

    bash scripts/benchmark-qwen3-tts.sh --voice Serena --repeats 10

## Optional 0.6B Base voice clone

The same sidecar supports the Base checkpoint. Start it with a reference WAV:

    QWEN3_TTS_MODEL=Qwen/Qwen3-TTS-12Hz-0.6B-Base QWEN3_TTS_REF_AUDIO=/c/path/to/reference.wav QWEN3_TTS_REF_TEXT='参考音频对应的准确文本。' bash scripts/start-qwen3-tts.sh --preload

If QWEN3_TTS_REF_TEXT is omitted, the sidecar builds an x-vector-only clone prompt. This is simpler but may reduce clone fidelity.

## VoiceDesign status — Workbench contract reserved

VoiceDesign is no longer treated as a realtime Provider and is not part of normal stack startup. Instead, TTS Workbench exposes a separate Voice Design tool surface.

Implemented in Character Memory:

- Voice Design UI in `:9002/tts`;
- sidecar status adapter;
- generate proxy;
- AI prompt polish using the standard Character Memory LLM configuration;
- fixed local sidecar contract at `:9015` by default.

Not implemented in Character Memory main environment:

- 1.7B model download;
- qwen-tts/Torch installation for VoiceDesign;
- CUDA preload;
- local sidecar process startup.

Local integration details are specified in `docs/current/QWEN3_VOICE_DESIGN_TOOL.md`.

## Acceptance questions

For production acceptance, answer these questions with real data:

1. Does the 0.6B model fit without OOM while the intended application workload is present?
2. What is resident VRAM after load?
3. What are warm P50/P95 inference latency and RTF for normal 15-30 character Chinese replies?
4. Does SDPA already meet latency requirements, or is FlashAttention worth the deployment complexity?
5. Is the audible improvement over the lightweight TTS route large enough to justify several GB of GPU residency?


## Current integration boundary

The sidecar code and setup/start scripts remain available for explicit experiments, but normal Character Memory startup does not launch Qwen3-TTS and formal chat cannot select it.

If VoiceDesign work is resumed later, build a dedicated tool/workflow around the isolated runtime instead of reintroducing it as a realtime chat provider.
