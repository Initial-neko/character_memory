# TTS Provider Lab

TTS Provider Lab is an experimental audition surface for comparing local TTS quality without changing the production chat voice path.

## Ports and scope

- Character Runtime: `http://127.0.0.1:8000`
- Media Runtime: `http://127.0.0.1:8001`
- Dev Console: `http://127.0.0.1:8002/dev`
- TTS Provider Lab: `http://127.0.0.1:9002/tts`
- Optional CosyVoice sidecar: `http://127.0.0.1:9012`

The lab currently exposes three provider families:

1. **Sherpa VITS** — current production baseline, proxied through Media Runtime `:8001`. The audition list keeps the locally verified female speaker IDs `0`, `2`, and `5`.
2. **Kokoro 82M v1.1 zh** — runs in the Character Memory Python 3.12 environment. The audition voices are `zf_001`, `zf_002`, `zf_003`, and `zf_004` from the v1.1-zh repository.
3. **CosyVoice 300M SFT** — isolated behind a sidecar on `:9012` because its dependency/runtime requirements differ from the main Python 3.12 environment.

The formal voice call still uses Media Runtime `/v1/tts`. Choosing a provider in this lab does **not** switch production chat TTS.

## Kokoro setup: download before runtime

Kokoro package dependencies now belong to the canonical `all` extra, but model weights and voice packs are deliberately prepared by a setup script rather than being downloaded by the first synthesis request.

From the repository root in Git Bash:

```bash
bash scripts/setup-tts-models.sh
```

That script performs the canonical `uv sync --extra all` and then prefetches the required Kokoro assets into the ignored project-local Hugging Face cache under `models/huggingface`:

- `config.json`
- `kokoro-v1_1-zh.pth`
- `voices/zf_001.pt`
- `voices/zf_002.pt`
- `voices/zf_003.pt`
- `voices/zf_004.pt`

After setup:

```bash
uv run character-stack --open tts
```

Open `http://127.0.0.1:9002/tts`.

Kokoro synthesis uses the prefetched local files directly. The request path does not download model or voice files. If the package stack or required cache files are missing, Provider status remains unavailable and tells you to rerun `bash scripts/setup-tts-models.sh`.

The old names `zf_xiaobei`, `zf_xiaoni`, `zf_xiaoxiao`, and `zf_xiaoyi` belong to the base `hexgrad/Kokoro-82M` voice inventory. They are not files in `hexgrad/Kokoro-82M-v1.1-zh`; the v1.1-zh lab therefore uses numbered `zf_*` packs.

The model repository is public. `HF_TOKEN` is optional, but setting it before setup can improve Hugging Face rate limits.

### CPU and GPU behavior

Prefetching removes request-time network/download latency, but it does not remove model initialization or inference cost. On the first Kokoro synthesis the process still has to deserialize the PyTorch weights, initialize Chinese G2P, and warm up inference. With the default configuration all Kokoro inference runs on CPU, so high CPU usage during first load and synthesis is expected.

To test GPU inference, first confirm the installed Torch build exposes CUDA, then run:

```bash
CHARACTER_TTS_KOKORO_DEVICE=cuda uv run character-stack --open tts
```

Do not assume CUDA is available merely because an NVIDIA GPU is installed; verify the Torch build first.

## CosyVoice 300M SFT sidecar

CosyVoice remains isolated from the main Python 3.12 environment. The official project recommends a dedicated Python 3.10 environment, so mixing it into the Character Memory dependency graph would make the main runtime fragile.

The current manual setup remains:

```bash
mkdir -p .external
git clone --recursive https://github.com/QwenAudio/CosyVoice.git .external/CosyVoice
uv venv .venv-cosyvoice --python 3.10
uv pip install --python .venv-cosyvoice/Scripts/python.exe \
  -r .external/CosyVoice/requirements.txt \
  -i https://mirrors.aliyun.com/pypi/simple/
uv pip install --python .venv-cosyvoice/Scripts/python.exe huggingface_hub
.venv-cosyvoice/Scripts/python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice-300M-SFT', local_dir='.external/CosyVoice/pretrained_models/CosyVoice-300M-SFT')"
```

Start its sidecar in another Git Bash window:

```bash
COSYVOICE_ROOT="$(pwd)/.external/CosyVoice" \
COSYVOICE_MODEL_DIR="$(pwd)/.external/CosyVoice/pretrained_models/CosyVoice-300M-SFT" \
.venv-cosyvoice/Scripts/python.exe scripts/cosyvoice_sidecar.py
```

Expected health endpoint: `http://127.0.0.1:9012/health`.

## Audition workflow

Use the same text for all providers. Useful comparison material includes short emotional sentences and longer conversational sentences:

```text
真的吗？那还挺不错的呀。
没关系，我只是有一点累。
刚才其实想了很久，不过后来觉得，有些事情可能没有想象中那么复杂。
```

The lab reports provider, voice, inference latency, generated audio duration, sample rate, device, and browser-observed HTTP duration. **生成全部可用 Provider** runs providers serially so large models do not compete for RAM/VRAM during comparison.

Prioritize base timbre, Mandarin pronunciation, sentence-final tone, punctuation/pause naturalness, long-sentence stability, warm inference latency, and resource use.

## Current decision boundary

```text
TTS Lab audition
    -> choose preferred provider + voice
    -> measure warm latency/RAM/VRAM
    -> set one production default voice
    -> later add per-character identity/emotion only if worthwhile
```

CI verifies API/UI contracts and dependency isolation. Actual audio quality, model download behavior, Windows native compatibility, and GPU performance require local live testing.
