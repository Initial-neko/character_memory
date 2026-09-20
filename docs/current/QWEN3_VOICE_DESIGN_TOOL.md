# Qwen3-TTS 1.7B VoiceDesign Workbench Contract

Status: complete. UI, the Character Memory adapter, and the `:9015` sidecar
(`src/character_memory/qwen3_voice_design_experiment.py`) are all implemented and
verified end-to-end. The sidecar is intentionally **not** auto-started by the main
stack — see "Starting the sidecar" below.

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

## Starting the sidecar

```bash
bash scripts/start-qwen3-voice-design.sh          # :9015, lazy — loads on first request
```

Measured on an RTX 5060 Laptop (8 GB), 2026-09-19:

| | value |
|---|---|
| model load | ~24 s |
| resident after load | 3994 MB allocated / 4132 MB reserved (4515 MiB process) |
| first generation after load | 22.8 s for 1.68 s audio (**warm-up: kernel autotune**) |
| steady state | 5.8–7.0 s for ~2.0 s audio, **RTF ~2.9–3.5** |
| `POST /v1/unload` | releases to 709 MiB; ~455 MB stays as the process CUDA context |

**The first generation in a session is 3–4× slower than the rest.** Warm the model
once before showing the panel to someone, or expect a ~23 s first click.

`/v1/unload` genuinely returns the card, which matters because the 1.7B model
cannot be resident together with GSV on an 8 GB card. GSV reloads in ~6 s
afterwards and works normally while an *idle* VoiceDesign process is still
running, so the steady state is: GSV loaded, VoiceDesign process up but unloaded.

### Endpoints

```text
GET  :9015/health           -> 200 always; {ready, loaded, model, device, reason}
POST :9015/v1/voice-design  -> WAV + X-Voice-Design-{Model,Device,Inference-Ms,Audio-Ms,Sample-Rate}
POST :9015/v1/load          -> explicit preload
POST :9015/v1/unload        -> release VRAM before switching back to GSV
```

There is deliberately **no** `/v1/configure`: there is one model, and its id is a
contract constant on both sides. Model choice belongs to `QWEN3_VOICE_DESIGN_MODEL`.
`/health` returns 200 even when `ready: false`, because the adapter reads a non-2xx
status as "sidecar not running" and would show the wrong remedy.

Designs are **not reproducible**: the same text and instruct gave 1.68 s, 1.92 s and
2.00 s of audio across four identical requests. Keep the WAV — do not expect to
regenerate the same voice.

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

No second API key or LLM configuration is introduced. Workbench calls the public `PersonModel.complete_text_for_session()` contract; it does not reach into the OpenAI-compatible adapter's private `_request()` transport method.

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
X-Voice-Design-Artifact
```

`X-Voice-Design-Artifact` is an opaque, server-generated token naming *this*
audition. It is the only handle accepted by `freeze` (below). The caller never
chooses it and it is not derived from the audio, so two identical auditions get
two distinct tokens. The Lab keeps the last **8** auditions; older ones are
evicted and freezing them returns 404.

### Freeze

`POST /v1/voice-design/freeze` turns an audition into a character's permanent voice:

```json
{ "character_id": "momo", "artifact_id": "5qlrisDLQfsUddO9ONOTvQ" }
```

The request is `extra="forbid"` and takes **no** text or audio: the reference
transcript is the text that actually produced the audition, so a client cannot
smuggle in a transcript that does not match the audio. That mismatch is the
hardest failure mode of zero-shot cloning, and this design removes it by
construction rather than by validation.

On success it writes, beside the character's `persona.yaml`:

```text
personas/<id>/voice/<sha256[:16]>.wav   # the auditioned bytes, verbatim
personas/<id>/voice.yaml                # the profile
```

The WAV is content-addressed, so re-freezing never overwrites an earlier clip.
That matters because **VoiceDesign is unseeded** — four identical requests
produce four different clips, so a frozen clip cannot be regenerated from its
prompt. It is an asset, not a recipe.

`voice.yaml`:

```yaml
voice_id: momo            # omit to default to the directory name
ref_audio: voice/ffa77b6a3acab112.wav   # relative to this file
ref_text: 你好，今天天气不错，我们出去走走吧。
gpt_model: null           # null inherits the global GSV model
sovits_model: null
# provenance, written by freeze; GSV does not read these, but a clip that
# cannot be regenerated is only explainable through its record:
created_at: '2026-09-19T15:26:34.472897+00:00'
instruct: 年轻女性，声音清亮柔和，语速偏慢，带一点温柔的笑意。
model: Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign
```

> The reader (`voices.py`) declares the provenance fields explicitly and keeps
> `extra="forbid"`, so a typo in a *contract* field still fails loudly. The two
> schemas must change together — see
> `tests/test_tts_lab_voice_freeze.py::test_frozen_voice_yaml_is_loadable_by_the_registry`,
> which round-trips the writer through the reader.

After writing, freeze asks the GSV sidecar to re-read the registry:

```text
POST http://127.0.0.1:9014/v1/voices/reload
```

This is a pure file re-read: no engine reload, no VRAM movement. New reference
audio is cached lazily on the next `infer_batched`, so **the voice is live on the
next chat turn**. The response reports what happened:

```json
{ "ok": true, "character_id": "momo", "voice_id": "momo",
  "ref_audio": "voice/ffa77b6a3acab112.wav", "ref_text": "你好…",
  "activated": true, "reason": null }
```

**Reload failure is non-fatal by design.** If GSV is not running, freeze still
writes both files and answers 200 with `activated: false` and a reason; the voice
registers on the next GSV start. Files are never half-written: the WAV lands
first, `voice.yaml` last, so a crash cannot leave a profile pointing at a missing
clip.

## Local sidecar contract

The local implementation should bind by default to:

```text
http://127.0.0.1:9015
```

Override from Character Memory with:

```text
CHARACTER_TTS_QWEN3_VOICE_DESIGN_BASE
```

The sidecar needs the two contract endpoints below, plus `/v1/load` and `/v1/unload`
for VRAM control (see "Starting the sidecar").

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

The generated audio is an audition artifact until it is frozen. The Workbench
enables "freeze to character" only while the live inputs still match the
snapshot the audition was generated from; **Freezing** above persists the bytes
and registers them with GSV.

This matters more than it looks, because VoiceDesign does **not** accept a seed: four
identical requests produced 1.68 s, 1.92 s, 2.00 s and 2.00 s of audio. A designed
voice cannot be regenerated from its `instruct` — **the WAV is the asset.** Any freeze
flow must persist the bytes, not the prompt that produced them.

Timing, measured (RTX 5060 Laptop, 8 GB):

| step | value |
|---|---|
| first generation after a load | 22.8 s for 1.68 s audio — cuDNN kernel autotune |
| steady state | 5.8–7.0 s for ~2.0 s audio (**RTF ~2.9–3.5**) |
| `POST /v1/unload` | 4515 MiB → 709 MiB |

Budget the first click at ~23 s, not ~6 s.
