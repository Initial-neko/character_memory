# Qwen3-TTS isolated runtime

This experiment measures whether Qwen3-TTS is worth integrating into Character Memory before it touches the formal Media Runtime path.

## Scope

- isolated sidecar on 127.0.0.1:9013
- isolated Python 3.12 environment: .venv-qwen3-tts
- default model: Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
- default attention: SDPA
- default CUDA dtype: FP16; auto resolves to FP16 on CUDA and FP32 on CPU
- explicit model load endpoint and lazy first-request loading
- latency, RTF and VRAM benchmark
- optional Base-model voice-clone mode

It is not wired into config.yaml, Media Runtime, browser voice calls, or the normal Character Memory stack yet.

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

    QWEN3_TTS_DEVICE=cuda:0 QWEN3_TTS_DTYPE=float16 QWEN3_TTS_ATTN=sdpa bash scripts/start-qwen3-tts.sh

For newer GPUs with good BF16 support:

    QWEN3_TTS_DTYPE=bfloat16 bash scripts/start-qwen3-tts.sh

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

## Acceptance questions

Do not integrate Qwen3-TTS into the formal TTS route until the target machine answers these questions with real data:

1. Does the 0.6B model fit without OOM while the intended application workload is present?
2. What is resident VRAM after load?
3. What are warm P50/P95 inference latency and RTF for normal 15-30 character Chinese replies?
4. Does SDPA already meet latency requirements, or is FlashAttention worth the deployment complexity?
5. Is the audible improvement over the lightweight TTS route large enough to justify several GB of GPU residency?


## Formal integration

Set `tts_provider: "qwen3"` and choose a Qwen3 voice such as `Vivian` in Settings Center. When the full stack starts, Character Memory launches the isolated `.venv-qwen3-tts` runtime on `:9013` automatically, and browser/chat TTS continues to call Media Runtime `:8001/v1/tts`.

The sidecar remains dependency-isolated: Torch/CUDA packages stay out of the core project environment.
