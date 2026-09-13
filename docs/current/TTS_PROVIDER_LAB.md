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
2. **Kokoro 82M v1.1 zh** — loaded lazily in the Character Memory Python 3.12 environment. Default audition voices are `zf_xiaobei`, `zf_xiaoni`, `zf_xiaoxiao`, and `zf_xiaoyi`.
3. **CosyVoice 300M SFT** — isolated behind a small sidecar on `:9012` because the official CosyVoice stack uses a different/heavier dependency environment. The lab discovers the SFT speaker list from `list_available_spks()` once the sidecar loads the model.

The formal voice call still uses Media Runtime `/v1/tts`. Choosing a provider in this lab does **not** silently switch production chat.

## Quick start: Sherpa + Kokoro

From the repository root in Git Bash:

```bash
uv sync --extra api --extra media --extra local-embedding --extra dev --extra tts-kokoro
uv run character-stack --open tts
```

Then open:

```text
http://127.0.0.1:9002/tts
```

Sherpa is ready when the existing Media Runtime TTS model is configured. Kokoro becomes ready when the `tts-kokoro` extra is installed. The first Kokoro synthesis may download `hexgrad/Kokoro-82M-v1.1-zh` and the selected voice from Hugging Face, so the first request can be materially slower than warm requests.

If GPU use is desired for Kokoro, start the stack with an explicit environment override after confirming the installed Torch build supports the GPU:

```bash
CHARACTER_TTS_KOKORO_DEVICE=cuda uv run character-stack --open tts
```

CPU remains the default because it is the safest cross-machine audition baseline.

## CosyVoice 300M SFT sidecar

CosyVoice is intentionally not added to Character Memory's normal `uv sync` dependency set. The official project recommends a dedicated Python 3.10 environment, while Character Memory is pinned to Python 3.12. Mixing these stacks would make the normal Voice/ASR development environment fragile.

### 1. Clone CosyVoice with submodules

From the Character Memory repository root:

```bash
mkdir -p .external
git clone --recursive https://github.com/QwenAudio/CosyVoice.git .external/CosyVoice
```

If the repository already exists:

```bash
git -C .external/CosyVoice submodule update --init --recursive
```

### 2. Create an isolated Python 3.10 environment

```bash
uv venv .venv-cosyvoice --python 3.10
```

On Windows/Git Bash the Python executable is normally:

```text
.venv-cosyvoice/Scripts/python.exe
```

Install the official CosyVoice requirements into that interpreter:

```bash
uv pip install --python .venv-cosyvoice/Scripts/python.exe \
  -r .external/CosyVoice/requirements.txt \
  -i https://mirrors.aliyun.com/pypi/simple/
```

CosyVoice has a substantially heavier native/Torch dependency stack than Sherpa. Windows installation can be machine-specific; keep any fixes inside `.venv-cosyvoice` rather than changing Character Memory's primary environment.

### 3. Download the SFT model

Install the Hugging Face client into the isolated environment if the CosyVoice requirements did not already provide it:

```bash
uv pip install --python .venv-cosyvoice/Scripts/python.exe huggingface_hub
```

Then download the fixed-speaker SFT model:

```bash
.venv-cosyvoice/Scripts/python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice-300M-SFT', local_dir='.external/CosyVoice/pretrained_models/CosyVoice-300M-SFT')"
```

### 4. Start the sidecar

In a second Git Bash window:

```bash
COSYVOICE_ROOT="$(pwd)/.external/CosyVoice" \
COSYVOICE_MODEL_DIR="$(pwd)/.external/CosyVoice/pretrained_models/CosyVoice-300M-SFT" \
.venv-cosyvoice/Scripts/python.exe scripts/cosyvoice_sidecar.py
```

Expected endpoint:

```text
http://127.0.0.1:9012/health
```

Return to `http://127.0.0.1:9002/tts` and click **刷新 Provider**. CosyVoice should move from `unavailable` to `ready`. Its first synthesis performs the actual model load; after that the status should report `loaded`.

If Git Bash path conversion causes a Windows path problem, convert paths explicitly before exporting them, for example:

```bash
export COSYVOICE_ROOT="$(cygpath -m "$(pwd)/.external/CosyVoice")"
export COSYVOICE_MODEL_DIR="$(cygpath -m "$(pwd)/.external/CosyVoice/pretrained_models/CosyVoice-300M-SFT")"
.venv-cosyvoice/Scripts/python.exe scripts/cosyvoice_sidecar.py
```

## Audition workflow

Use the same text for all providers. Good comparison material includes both short emotional sentences and longer conversational sentences, for example:

```text
真的吗？那还挺不错的呀。
没关系，我只是有一点累。
刚才其实想了很久，不过后来觉得，有些事情可能没有想象中那么复杂。
```

The lab shows provider, voice, inference latency, generated audio duration, sample rate, device, and browser-observed HTTP duration. **生成全部可用 Provider** runs providers serially rather than concurrently so multiple large TTS models do not compete for RAM/VRAM during comparison.

During evaluation, prioritize:

- whether the base timbre is pleasant enough for long conversations;
- Mandarin pronunciation and sentence-final tone;
- punctuation/pause naturalness;
- long-sentence stability;
- warm inference latency and resource use;
- whether a single default voice is good enough before investing in per-character voice identity.

## Current decision boundary

This lab is deliberately provider-neutral. The next production step should happen only after listening tests:

```text
TTS Lab audition
    -> choose preferred provider + voice
    -> measure warm latency/RAM/VRAM
    -> set one production default voice
    -> later add per-character voice identity/emotion only if worthwhile
```

Do not infer provider quality from CI. CI verifies API/UI contracts and dependency isolation; actual Kokoro/CosyVoice audio quality, model download behavior, and Windows GPU compatibility require local live testing.
