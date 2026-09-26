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

## 2. Configuration levels

Every schema field and every secret carries a **level**, served in `GET /v1/settings`.
The page renders `common` inline and the other two as real, closed `<details>`
groups, so what the first screen shows is decided by the schema and not by the
frontend.

```text
common      8 fields + the two keys a user must supply   -> first screen
advanced   15 fields                                     -> one closed group
diagnostic 29 fields                                     -> one closed group
```

Where a field goes:

```text
common     nothing works, or nothing is understandable, without the user deciding
           (chat temperature, TTS provider/voice/speed, the autonomy switches,
            the LLM and embedding API keys)
advanced   a defensible default exists, but the value changes behaviour
           (chat/vision model, GSV runtime assets, provider choice, intervals,
            recall limit, voice-call pause)
diagnostic transport, path, ceiling and polling plumbing -- the first place to
           look when something is wrong (base URLs, attempt count, timeouts,
           browser channel, storage paths, poll intervals, per-day ceilings)
```

A field without a `level` is served as `diagnostic`. The default is deliberately
the *hidden* level: a new field that forgets to declare one is under-exposed
rather than promoted onto the first screen the user opens on.

Levels are a projection of the schema, not a claim that a hidden field is
unsupported. Every field stays reachable by opening its group, and each group's
summary names what is inside it (`高级设置（2 项）：Chat Model · Vision Model`) --
never "更多设置". Secondary actions follow the same rule: `重新读取 / 刷新健康状态`
is advanced, `迁移旧 config Key` is diagnostic, and `保存配置` is common.

### 2.1 Secrets follow the selected providers

Secrets carry levels too, and two of them depend on the current selection:

```text
OPENCODE_GO_API_KEY / EMBEDDING_API_KEY        common
the selected provider's key                    advanced   (search_provider,
                                                           image_generation_provider)
the other providers' keys + HF_TOKEN           diagnostic
```

Only the key of the provider that is selected stays on the advanced level; the
other provider's key drops into the diagnostic group instead of being removed,
because a key has to exist *before* its provider can be switched to. Within a
level, configured keys sort before unconfigured ones.

### 2.2 Restart requirement is part of the field

`restart_required` is served per field (`true` unless the field is in
`HOT_APPLY_FIELDS`) and the page marks those fields with a `需重启` chip next to
the label. The save response still lists only the fields that save actually
changed, but the information no longer vanishes with the toast.

## 3. Persistence guarantees

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

### 3.1 Character Space test controls

Settings Center exposes a dedicated **Character Space** card for the current test stage:

```yaml
space_autonomy_enabled: true
space_opportunity_interval_minutes: 1440
space_max_posts_per_day: 0
space_media_enabled: true
space_media_max_items: 3
space_image_search_enabled: true
space_image_generation_enabled: true
space_world_observation_enabled: true
space_world_max_pages: 2
space_world_max_chars_per_page: 6000
space_audience_size: 5
space_scheduler_poll_seconds: 60
```

The interval accepts `10..10080` minutes. `1440` is the normal 24H default; `60` is the recommended 1H soak-test preset. A shorter interval lets one character publish several posts in a day — an Opportunity is a chance to decide, not an obligation, so an interval is not a posting rate. `space_max_posts_per_day` accepts `0..200` and is the publishing ceiling per character per local day; `0` is the default and means no ceiling, which is safe because the interval already paces publishing. Space media is independently gated from posting: space_media_enabled=false keeps text autonomy intact; space_media_max_items accepts 0..9 and caps actual image execution per post; image search and AI ImageGen can be disabled separately. World Observation is separately gated: when enabled the character may choose whether to explore a public topic, with space_world_max_pages (1..4) and space_world_max_chars_per_page (500..16000) bounding browser cost. Search/browse never forces memory or a post. These are execution ceilings/gates only—the character still decides whether media/exploration is natural. Audience size accepts 0..10; `0` means the character may still autonomously post but the post is not automatically distributed to other characters. The hard audience ceiling remains 10.

These fields persist in `config.yaml`. Settings Center owns the formal values. Dev Console may hot-apply temporary Space / Group Autonomy overrides to the current Character Runtime for testing, but it does not write those temporary values back to `config.yaml`. Changing the poll interval changes scheduler latency only; it never changes the Opportunity interval.

### 3.2 Random Encounter

Settings Center owns the persisted Random Encounter configuration:

```yaml
encounter_enabled: true
encounter_interval_minutes: 1440
encounter_web_probability: 0.5
encounter_max_pending: 3
encounter_poll_seconds: 60
```

The enabled flag, interval, and web/generated mix are advanced settings; pending and poll limits stay diagnostic. Dev Console exposes only status/manual-trigger diagnostics and never becomes a second persisted editor for these fields.

### 3.3 World Activity

World Activity has its own formal Settings section because going online is no longer tied to Space publishing:

```yaml
world_activity_enabled: true
world_pulse_enabled: true
world_pulse_sources:
  - https://tophub.today/
  - https://news.ycombinator.com/
  - https://github.com/trending
world_pulse_refresh_minutes: 60
world_pulse_discussion_interval_minutes: 360
world_pulse_max_topics: 8
world_pulse_commenter_count: 4
world_browse_enabled: true
world_browse_interval_minutes: 30
world_pulse_source_max_chars: 8000
world_browse_max_pages: 2
world_activity_poll_seconds: 60
```

Behavior switches, source pages and user-visible cadences are advanced; text/page/poll ceilings stay diagnostic. `Pulse Sources` is edited one public URL per line and persists as a real YAML list. Dev Console has no World Activity save controls: it only refreshes, discusses, browses and runs due work against the Settings-owned values.

### 3.4 Proactive Intent

Due Intents get their own section because the dispatch cadence used to be a module constant with no cooldown, which let a 30-second poll spend a whole reaction every round:

```yaml
proactive_dispatch_enabled: true
proactive_poll_seconds: 30
proactive_min_dispatch_interval_minutes: 60
proactive_intent_min_delay_minutes: 10
proactive_max_pending_intents: 20
proactive_intent_dedup_enabled: true
proactive_intent_duplicate_similarity: 0.90
proactive_intent_dedup_window_hours: 72
```

`proactive_dispatch_enabled` defaults to `true`, so the shipped behavior is unchanged — the switch exists so the loop can be stopped without editing source. `proactive_poll_seconds` is check latency only, the same distinction `space_scheduler_poll_seconds` makes.

The rest are not suggestions the model may exceed. The cooldown, the delay floor and the pending ceiling are enforced on the write and dispatch paths and are documented in `PERSON_RUNTIME.md` §10; the similarity threshold and the dedup window are the tuning knobs for the duplicate rule. Dispatch switches and the interval are advanced; the poll, the similarity threshold and the dedup window are diagnostic.

None of these fields are hot-applied: like the Space and World scheduling values, they take effect after a restart. The dispatch loop reads `proactive_dispatch_enabled` on every tick, so turning it off is immediate even though turning it back on is not.

## 4. Search / World Browser

The Search / ImageGen card also owns the headless World Browser transport settings:

    web_browser_channel: auto          # auto | chromium | chrome
    web_browser_timeout_seconds: 20
    web_browser_render_wait_ms: 700

auto first tries Playwright-managed Chromium and then installed Chrome. These settings control page rendering only; SearchAPI/Brave still own URL discovery. Changing them requires Character Runtime restart through the normal Settings semantics.

Playwright's Python package is part of the canonical all environment. A managed Chromium binary is installed separately with:

    uv run playwright install chromium

Without managed Chromium, auto may use an installed desktop Chrome. If neither browser is available, World Observation fails soft and normal Space autonomy continues.

## 5. Formal TTS selection

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

## 6. Hot-apply semantics

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

## 7. GSV runtime configuration

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

## 8. Legacy secret migration

Recognized legacy plaintext fields include:

```text
api_key            -> OPENCODE_GO_API_KEY
embedding_api_key  -> EMBEDDING_API_KEY
search_api_key     -> SEARCHAPI_API_KEY or BRAVE_SEARCH_API_KEY
agnes_api_key      -> AGNES_API_KEY
msimg_api_key      -> MSIMG_API_KEY
```

Migration writes the secret to `.env`, removes plaintext YAML values, and keeps only a sanitized YAML backup. Settings never creates a historical `.env.bak.*` chain.

## 9. HTTP surface

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

## 10. Startup

```bash
bash scripts/setup-media-models.sh
uv run character-stack --open settings
```

The canonical setup also prefetches the local BGE embedding because the normal Character Runtime is strict-offline for sentence-transformers models.
