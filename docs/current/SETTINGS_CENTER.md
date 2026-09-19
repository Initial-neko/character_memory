# Settings Center

Settings Center is the single configuration management surface for Character Memory local runtime.

## Ports and ownership

```text
Chat              :8000
Media Runtime     :8001
Dev Console       :8002
Settings Center   :8003/settings
TTS Provider Lab  :9002/tts
```

Settings Center owns **configuration persistence**, not model/runtime execution. Runtime services load the persisted configuration on startup.

## Source of truth

V1 deliberately keeps two persistence files:

```text
config.yaml   non-sensitive runtime configuration
.env          API keys / tokens only
```

Effective secret precedence is:

```text
system environment > .env > legacy config.yaml value
```

The final `legacy config.yaml` level exists only for backward compatibility. When Settings Center sees plaintext legacy secret fields, it migrates them to `.env` and removes the plaintext fields from `config.yaml`.

The browser never receives existing secret values. Secret status contains only metadata such as `configured`, `source`, and `stored_in_env`.

## Legacy secret migration

Recognized migration fields:

```text
api_key            -> OPENCODE_GO_API_KEY
embedding_api_key  -> EMBEDDING_API_KEY
search_api_key     -> SEARCHAPI_API_KEY or BRAVE_SEARCH_API_KEY
agnes_api_key      -> AGNES_API_KEY
msimg_api_key      -> MSIMG_API_KEY
```

`search_api_key` uses the configured `search_provider` to choose its target environment name.

Migration order is intentionally safe:

1. write the secret to `.env` when needed;
2. verify/persist the environment file;
3. remove plaintext secret keys from `config.yaml`;
4. write a **sanitized** config backup.

Settings Center never creates a historical `.env.bak.*` chain. This avoids multiplying plaintext credentials on disk.

## Normal config save

The configuration editor is schema-driven in the UI, but V1 preserves the existing flat `config.yaml` contract so current runtime code stays compatible.

On every changed normal-config save:

```text
config.yaml
  -> config.yaml.bak.YYYYMMDD-HHMMSS
  -> validate complete Settings model
  -> patch only edited top-level keys
  -> atomic replace config.yaml
```

Comments, unknown extension keys, and unrelated ordering are preserved. Settings Center does not reserialize the whole YAML file with `safe_dump`.

V1 has an explicit restart policy: persisted changes are not partially hot-reloaded. Stop the current stack and start it again so Character Runtime, Media Runtime, Dev Console, Settings Center and TTS Provider Lab read one coherent snapshot.

## Formal chat TTS

Formal browser voice still calls the stable Media Runtime endpoint:

```text
Browser -> :8001/v1/tts
```

Media Runtime reads:

```yaml
tts_provider: kokoro
tts_voice: zf_001
tts_speed: 1.0
tts_device: cpu
```

Kokoro is the V1 default after audition. For `tts_provider: kokoro`, Media Runtime forwards synthesis to the local provider service exposed by `:9002/v1/tts`; the browser never needs provider-specific URLs or extra CORS rules. Sherpa remains selectable as a fallback/default through Settings.

TTS Provider Lab remains the audition/benchmark UI. Changing a dropdown in the lab does not persist the production default; change the production default in Settings Center.

### Health-gated TTS selection

Settings Center does not trust a static provider list for TTS selection. On every settings refresh it probes only the formal realtime providers individually through `:9002/v1/providers/{provider_id}`: Kokoro, Sherpa, Edge and GSV. One failed/slow provider no longer invalidates the whole inventory.

The Voice section therefore behaves as follows:

- only providers with `ready=true` are selectable;
- unhealthy/unavailable providers remain visible but disabled, with the provider reason shown in the UI;
- the Voice dropdown is rebuilt from the selected healthy provider's reported `voices`;
- switching Provider automatically selects that provider's `default_voice` and reconciles the Device control with the running provider (`cuda`/`cpu`/cloud);
- save performs the same health/voice validation again on the server; when Provider changes, stale incompatible Voice/Device values are normalized to the healthy provider defaults instead of failing the save;
- changing unrelated settings is still allowed when the currently configured TTS happens to be unavailable.

Formal voice selection is now consistently persisted in `tts_voice`. For GSV, the sidecar reports the voice identity associated with its configured reference (currently `murasame`); Settings writes that reported voice into `tts_voice`, and Media Runtime sends the same value during synthesis.

`tts_device` is used by local providers where supported. Edge reports `cloud`, so the Device control switches to `Cloud (Provider managed)` and is disabled while Edge is selected.

The Voice section also exposes a **测试当前 TTS** action. It calls Settings Center `POST /v1/tts-preview`, which rechecks provider health and then proxies the selected Provider/Voice/Speed to `:9002/v1/tts`. This is an audition only; it does not persist configuration.

### Health-checkable local providers without eager GPU model load

GSV sidecar starts when its isolated environment and required asset environment variables exist. It preloads only when `tts_provider: gsv`; otherwise it can report readiness without loading the model. If selected GSV assets are missing, stack startup fails explicitly.

Qwen3-TTS is not part of the formal provider inventory and is not started by normal `character-stack`; it remains manual experimental/future VoiceDesign tooling.

## Startup

Canonical local startup:

```bash
bash scripts/setup-media-models.sh
uv run character-stack
```

Open Settings directly with:

```bash
uv run character-stack --open settings
```

or:

```text
http://127.0.0.1:8003/settings
```

Chat and TTS Lab both link to the Settings Center.

## HTTP surface

```text
GET    /health
GET    /settings
GET    /v1/settings
PATCH  /v1/settings
PUT    /v1/settings/secrets/{ENV_NAME}
DELETE /v1/settings/secrets/{ENV_NAME}
POST   /v1/settings/migrate
GET    /v1/runtime-status
```

Secret mutation endpoints accept only the explicit allowlist in `settings_store.py`. Existing secret values are never returned by the API.


## TTS hot-apply and GSV runtime persistence

Formal TTS selection stays in `config.yaml`:

```yaml
tts_provider: gsv
tts_voice: murasame
tts_speed: 1.0
tts_device: cuda
```

GSV model/reference assets are provider runtime configuration and are persisted separately in the project-local `.env`:

```text
GSV_TTS_GPT_MODEL
GSV_TTS_SOVITS_MODEL
GSV_TTS_REF_AUDIO
GSV_TTS_REF_TEXT
GSV_TTS_VOICE
```

Settings Center edits these values without replacing the rest of `.env`; `upsert_env_value` updates only the named key and preserves unrelated variables. The YAML editor likewise patches only changed top-level fields and preserves comments/unknown keys.

TTS changes no longer require restarting the whole stack:

- Media Runtime reloads `tts_provider / tts_voice / tts_speed / tts_device` from the config file for each TTS request and health response.
- GSV sidecar exposes `POST /v1/configure`, `POST /v1/load`, and `POST /v1/unload`.
- Saving GSV model/reference fields pushes the new values into the already-running `:9014` process.
- Selecting GSV loads it immediately; switching away unloads GSV to release VRAM.
- `character-stack` reads project `.env` on startup and starts the GSV sidecar whenever its isolated venv exists, even when the asset fields are still incomplete. Incomplete GSV configuration therefore disables GSV instead of preventing Settings Center from starting.

Other non-TTS runtime/storage settings may still report `restart_required`; only those fields require process restart.
