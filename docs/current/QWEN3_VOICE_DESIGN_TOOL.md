# Qwen3-TTS 1.7B VoiceDesign Workbench Contract

Status: UI + Character Memory adapter contract are implemented. The heavy local VoiceDesign runtime is intentionally **not** implemented or auto-started in the main stack; local integration should provide the sidecar described below.

## Product role

Qwen3-TTS 1.7B VoiceDesign is a **voice creation tool**, not a realtime chat TTS provider.

It must not appear in:

- `config.yaml: tts_provider`;
- Settings Center realtime Provider selector;
- Media Runtime formal chat routing;
- realtime A/B Provider list.

It lives in the TTS Workbench as a separate tool:

```text
TTS Workbench :9002/tts
  -> Voice Design panel
  -> :9002/v1/voice-design/*
  -> optional local sidecar :9015
  -> Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign
```

Normal `character-stack` does not start or preload the 1.7B model.

## Workbench HTTP surface

Character Memory owns these stable endpoints:

```text
GET  :9002/v1/voice-design/status
POST :9002/v1/voice-design/polish
POST :9002/v1/voice-design/generate
```

### Status

`GET /v1/voice-design/status` returns the sidecar state without registering it as a TTS Provider.

Example:

```json
{
  "voice_design": {
    "id": "qwen3-voice-design",
    "label": "Qwen3-TTS 1.7B VoiceDesign",
    "ready": true,
    "loaded": true,
    "model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "device": "cuda:0",
    "reason": null,
    "base_url": "http://127.0.0.1:9015"
  }
}
```

### AI prompt polish

`POST /v1/voice-design/polish`:

```json
{
  "description": "年轻一点，温柔，软一点，不要太嗲，有一点慵懒感",
  "language": "Chinese"
}
```

Response:

```json
{
  "ok": true,
  "instruct": "年轻女性声线，音色清亮柔和，略带慵懒感……",
  "model": "deepseek-flash",
  "total_ms": 1234.5
}
```

This endpoint reuses the standard Character Memory LLM configuration:

```text
OPENCODE_GO_API_KEY
base_url
chat_model
```

No second API key or LLM configuration is introduced.

### Generate

`POST /v1/voice-design/generate`:

```json
{
  "text": "你好，这是 Character Memory 的新声线试听。",
  "language": "Chinese",
  "instruct": "年轻女性声线，音色清亮柔和，略带慵懒感。",
  "max_new_tokens": 2048
}
```

The response is audio bytes, normally `audio/wav`, with normalized Workbench headers:

```text
X-Voice-Design-Model
X-Voice-Design-Device
X-Voice-Design-Inference-Ms
X-Voice-Design-Audio-Ms
X-Voice-Design-Sample-Rate
```

## Local sidecar contract

The local implementation should bind by default to:

```text
http://127.0.0.1:9015
```

Override from Character Memory with:

```text
CHARACTER_TTS_QWEN3_VOICE_DESIGN_BASE
```

The sidecar only needs two endpoints.

### GET /health

Recommended response:

```json
{
  "ready": true,
  "loaded": true,
  "model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
  "device": "cuda:0",
  "reason": null
}
```

Semantics:

- `ready=true`: the sidecar can accept a generation request;
- `loaded=true`: the model is already resident;
- `reason`: human-readable failure reason when unavailable.

### POST /v1/voice-design

Request body is exactly the Workbench generate payload:

```json
{
  "text": "待合成文本",
  "language": "Chinese",
  "instruct": "声线描述",
  "max_new_tokens": 2048
}
```

The local implementation should call the official VoiceDesign model conceptually as:

```python
wavs, sr = model.generate_voice_design(
    text=text,
    language=language,
    instruct=instruct,
    max_new_tokens=max_new_tokens,
)
```

Return WAV bytes. Preferred response headers:

```text
Content-Type: audio/wav
X-Voice-Design-Model: Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign
X-Voice-Design-Device: cuda:0
X-Voice-Design-Inference-Ms: 3200.0
X-Voice-Design-Audio-Ms: 2800.0
X-Voice-Design-Sample-Rate: 24000
```

For compatibility, Character Memory's adapter also accepts the existing `X-TTS-*` metric header names.

## Local implementation constraints

The local sidecar should remain isolated from the main Character Memory Python environment.

Recommended boundary:

```text
main Character Memory .venv
  - no qwen-tts
  - no VoiceDesign model
  - no extra Torch requirement

separate VoiceDesign environment
  - qwen-tts
  - CUDA/Torch
  - Qwen3-TTS 1.7B VoiceDesign assets
```

Runtime should be local/offline once assets are prepared. Do not make a generation request depend on Hugging Face network access. The local integration may choose its own model directory, dtype, attention implementation and preload policy.

## UI workflow

The Workbench flow is:

```text
raw voice description
  -> AI 润色 (optional)
  -> editable instruct
  -> test text + language
  -> Generate Voice Design
  -> audio audition + runtime metadata
```

The generated audio is currently an audition artifact only. It is **not yet** automatically persisted into a voice registry or attached to GSV. That can be added later after the local VoiceDesign runtime is proven useful.
