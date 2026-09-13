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
