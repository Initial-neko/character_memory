# Settings Center

Settings Center (`:8003/settings`) is the persistent configuration surface for Character Memory. Source code remains authoritative; this document describes the current `main` contract.

## 1. Ownership

```text
config.yaml
  -> application/runtime selection and ordinary non-secret settings

.env
  -> API keys/tokens
  -> GSV-TTS-Lite model paths and default template name
```

The two files intentionally have different ownership. `config.yaml` answers **what the application selects**; provider-specific GSV model paths do not belong there.

Effective secret/runtime-env precedence is:

```text
real system environment > adjacent project .env > legacy config.yaml secret
```

`character-stack` does **not** promote the whole project `.env` into every child process. Doing so would make persisted values look like immutable system overrides and would hide later Settings edits. Only the GSV sidecar receives its persisted `GSV_TTS_*` values as process environment because the upstream runtime consumes that contract.

## 2. Persistence guarantees

Normal YAML saves:

```text
validate complete Settings model
  -> patch only edited top-level keys
  -> preserve comments / unknown extension keys / ordering
  -> atomic replace
  -> config.yaml.bak.YYYYMMDD-HHMMSS
```

GSV runtime fields are updated in `.env` as one atomic multi-key edit. Unrelated variables are preserved. If the second persistence surface fails, Settings rolls the first surface back so one logical save does not leave `config.yaml` and `.env` describing different states.

Existing secrets are never returned to the browser. Secret status exposes only metadata such as `configured`, `source`, and `stored_in_env`.

### 2.1 Character Space test controls

Settings Center exposes a dedicated **Character Space** card for the current test stage:

```yaml
space_autonomy_enabled: true
space_opportunity_interval_minutes: 1440
space_max_posts_per_day: 0
space_audience_size: 5
space_scheduler_poll_seconds: 60
```

The interval accepts `10..10080` minutes. `1440` is the normal 24H default; `60` is the recommended 1H soak-test preset. A shorter interval lets one character publish several posts in a day — an Opportunity is a chance to decide, not an obligation, so an interval is not a posting rate. `space_max_posts_per_day` accepts `0..200` and is the publishing ceiling per character per local day; `0` is the default and means no ceiling, which is safe because the interval already paces publishing. Audience size accepts `0..10`; `0` means the character may still autonomously post but the post is not automatically distributed to other characters. The hard audience ceiling remains 10.

These fields persist in `config.yaml`. Settings Center changes still report a Character Runtime restart requirement; Dev Console can persist the same values and hot-apply them immediately for testing. Changing the poll interval changes scheduler latency only; it never changes the Opportunity interval.

## 3. Formal TTS selection

Formal browser voice always calls:

```text
Browser -> Media Runtime :8001/v1/tts
```

The selector lives in `config.yaml`:

```yaml
tts_provider: gsv        # kokoro | sherpa | edge | gsv
tts_voice: murasame
tts_speed: 1.0
tts_device: cuda
```

`tts_voice` is a cross-provider field (a kokoro voice name, a sherpa speaker id) and stays one. It does not select GSV's default voice: the browser always sends `voice: <character id>`, so a character with no voice of its own lands on the **default template** (`GSV_TTS_VOICE`), not on `tts_voice`.

Qwen3-TTS is not a formal realtime Provider. Qwen3 VoiceDesign remains a Workbench tool.

Provider metadata is centralized in `character_memory.tts_registry`; Config validation, Settings inventory and Media routing consume the same formal provider IDs.

### Health-gated Provider and Voice

Settings probes the four formal providers individually through `:9002/v1/providers/{id}`.

- only `ready=true` providers are selectable;
- Voice options come from that provider's runtime health;
- changing Provider chooses its reported default Voice;
- server-side validation repeats the health/voice check on save;
- stale Voice/Device values from the previous Provider are normalized during Provider switching;
- one unavailable Provider cannot hide the healthy providers.

The Voice card also has **测试当前 TTS**, which calls `POST /v1/tts-preview` and performs a real synthesis through `:9002`.

## 4. Hot-apply semantics

Not every field called “device” can truthfully hot-apply.

| Change | Current behavior |
| --- | --- |
| TTS Provider | hot on next `:8001/v1/tts` request |
| TTS Voice | hot |
| TTS Speed | hot |
| GSV GPT/SoVITS model + default-template runtime values | `:9014 /v1/configure`, hot |
| GSV Device | unload/reconfigure/reload, hot |
| Kokoro Device | persisted; restart **TTS Provider Runtime :9002** |
| Sherpa Device | persisted; restart **Media Runtime :8001** |
| Edge Device | cloud-managed; local device control disabled |
| ordinary LLM/storage settings | restart requirement is returned explicitly |

Media Runtime reloads the current formal TTS selector for every health/synthesis request. It does not claim that an already-instantiated local model moved devices merely because YAML changed.

The save response separates persistence from runtime application:

```json
{
  "result": {
    "persisted": true,
    "restart_required": [],
    "runtime_apply": {
      "attempted": true,
      "applied": true,
      "error": null,
      "restart_required": []
    }
  }
}
```

If persistence succeeds but a GSV reload fails (for example CUDA OOM), Settings returns the durable configuration together with `runtime_apply.applied=false`. It does not misreport that situation as “save failed”.

## 5. GSV runtime configuration

These values persist in the project `.env`:

```text
GSV_TTS_GPT_MODEL
GSV_TTS_SOVITS_MODEL
GSV_TTS_VOICE
```

The two model paths are required, and every template inherits them unless it pins its own. `GSV_TTS_VOICE` names the **default template**, picked from a dropdown of `voices/*.yaml` (default `murasame`); GSV is ready once that template exists and its `ref_audio` resolves. The reference clip is therefore not a setting any more.

Voices themselves are templates, and a character only names one:

```text
voices/<name>.yaml                    # ref_audio + ref_text: the only carrier
voices/<name>/<content-addressed>.wav
personas/<character>/voice.yaml       # one line: template: <name>
```

A GSV sidecar can start health-checkable with incomplete global assets. Settings can fill the runtime configuration through `POST :9014/v1/configure`; selecting GSV preloads it, and switching away unloads it to release VRAM.

A normal Settings workflow therefore does not require shell exports. Manual `GSV_TTS_*` system variables remain deployment overrides and intentionally win over project `.env`.

## 6. Legacy secret migration

Recognized legacy plaintext fields include:

```text
api_key            -> OPENCODE_GO_API_KEY
embedding_api_key  -> EMBEDDING_API_KEY
search_api_key     -> SEARCHAPI_API_KEY or BRAVE_SEARCH_API_KEY
agnes_api_key      -> AGNES_API_KEY
msimg_api_key      -> MSIMG_API_KEY
```

Migration writes the secret to `.env`, removes plaintext YAML values, and keeps only a sanitized YAML backup. Settings never creates a historical `.env.bak.*` chain.

## 7. HTTP surface

```text
GET    /health
GET    /settings
GET    /v1/settings
PATCH  /v1/settings
POST   /v1/tts-preview
PUT    /v1/settings/secrets/{ENV_NAME}
DELETE /v1/settings/secrets/{ENV_NAME}
POST   /v1/settings/migrate
GET    /v1/runtime-status
```

## 8. Startup

```bash
bash scripts/setup-media-models.sh
uv run character-stack --open settings
```

The canonical setup also prefetches the local BGE embedding because the normal Character Runtime is strict-offline for sentence-transformers models.
