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

Settings Center does not trust a static provider list for TTS selection. On every settings refresh it asks `:9002/v1/providers` for the current provider inventory and health state.

The Voice section therefore behaves as follows:

- only providers with `ready=true` are selectable;
- unhealthy/unavailable providers remain visible but disabled, with the provider reason shown in the UI;
- the Voice dropdown is rebuilt from the selected healthy provider's reported `voices`;
- switching Provider automatically selects that provider's `default_voice`;
- save performs the same health/voice validation again on the server, so stale browser state cannot persist an unhealthy provider;
- changing unrelated settings is still allowed when the currently configured TTS happens to be unavailable.

Formal voice selection is now consistently persisted in `tts_voice`. For GSV, the sidecar reports the voice identity associated with its configured reference (currently `murasame`); Settings writes that reported voice into `tts_voice`, and Media Runtime sends the same value during synthesis.

`tts_device` is used by local providers where supported. Edge reports `cloud`, so the Device control is disabled while Edge is selected.

### Health-checkable sidecars without eager GPU model load

To make an unselected local provider discoverable without occupying model VRAM, `character-stack` may start prepared Qwen3/GSV sidecar processes in a health-only/lazy state:

- Qwen3 sidecar starts when its isolated environment exists, but does not load the model merely for health;
- GSV sidecar starts when its isolated environment and required asset environment variables exist;
- GSV preloads only when `tts_provider: gsv`; otherwise it reports readiness without loading the model;
- if a selected provider's required environment/assets are missing, stack startup still fails explicitly.

This allows Settings to offer only providers that are actually runnable while avoiding eager residency of every local TTS model.

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
