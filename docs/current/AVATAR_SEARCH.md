# Avatar Search & Avatar Sources

Avatar is a local Character asset. Current product supports several ways to obtain a candidate, but all selected avatars are ultimately copied into local avatar storage so current identity does not depend on an external URL remaining alive.

## 1. Current avatar sources

Current avatar can come from:

1. external image search candidate;
2. generated avatar candidate;
3. an existing chat/generated MediaAsset;
4. a Character Image Catalog asset.

Search and generation are different capabilities：

```text
Avatar Search
  -> short web image query
  -> external candidate
  -> validate/download
  -> local avatar asset

Avatar Generate
  -> character visual prompt
  -> ImageGen provider
  -> generated MediaAsset candidate
  -> explicit user selection
  -> local avatar asset
```

Neither path should silently replace the current avatar without user action.

## 2. Avatar Search flow

1. Web UI opens avatar manager from Character header/avatar.
2. Optional user text is a **preference hint**, not a raw search-engine query.
3. `AvatarIntentPlanner` asks the existing Character model what visual direction fits the person now, with bounded context:
   - Persona
   - current Mental State
   - at most a few recent short chat lines
   - optional preference
4. Planner returns compact search intent:
   - `visual_intent`
   - 1~3 short image-search queries
   - optional mood/style labels
5. Search service queries the primary query first and only spends extra provider requests when needed.
6. Provider results are normalized into opaque candidate IDs.
7. Browser chooses `search_id/candidate_id`, not arbitrary download URL.
8. Backend downloads the cached candidate, validates content type/size and stores a local avatar copy.
9. Character profile exposes a versioned local `/avatar/asset` URL.

If LLM planning fails, deterministic fallback search remains available so avatar management does not become unusable.

## 3. Privacy boundary

Persona, Mental State and recent dialogue can be used inside the local/server-side planner.

The external search provider only receives short generated search queries.

Planner instructions must not copy：

- user names/private identifiers；
- relationship secrets；
- long chat quotations；
- unrelated Memory content。

Avatar search intent is ephemeral tool context. It does not automatically become Character Memory or a normal PersonRuntime action.

## 4. Search configuration

Non-sensitive settings stay in `config.yaml`：

```yaml
search_provider: "searchapi"
search_country: "jp"
search_language: "zh-cn"
search_safe_search: "strict"
avatar_dir: ""
avatar_max_bytes: 8388608
```

Search credentials belong in `.env` or the Settings Center, not in new `config.yaml` examples：

```dotenv
SEARCHAPI_API_KEY=...
BRAVE_SEARCH_API_KEY=...
```

Effective secret precedence is documented in [`SETTINGS_CENTER.md`](SETTINGS_CENTER.md)：

```text
system environment > .env > legacy config.yaml secret
```

`search_api_key` in an old local config is only a backward-compatibility migration source. Settings Center migrates it to the provider-specific environment name and removes the plaintext field.

Brave alternative non-secret config：

```yaml
search_provider: "brave"
search_country: "ALL"
search_language: "zh"
search_safe_search: "strict"
```

## 5. Provider abstraction

Image search providers normalize results into a shared result shape including：

- original image URL；
- thumbnail URL；
- source page URL/domain；
- width/height when available。

This keeps SSRF/download validation, avatar persistence and UI selection independent from a specific search vendor.

`SearchProvider` is now shared infrastructure, not Avatar-owned infrastructure:

```text
SearchProvider
├─ search_images -> Avatar Search / Space image expression
└─ search_web    -> World Observation URL discovery

HeadlessBrowserWebFetcher
└─ rendered public-page observation for World Observation
```

This still does **not** expose a generic browser tool to Direct/Group chat. World Observation is a separate bounded product path, and raw page content is not automatically Memory or a chat message. Provider construction lives in `runtime_services.py`; `avatar_web.py` only consumes the already-composed service.

## 6. Generated avatar

Image generation routes：

```text
POST /v1/characters/{character_id}/avatar/generate
POST /v1/characters/{character_id}/avatar/from-chat
```

Generate compiles Persona/Mental State/recent mood into a plain-text visual prompt and calls the configured ImageGen provider.

If Provider supports reference images, current avatar may be supplied as an identity anchor.

Generated result is a **candidate**, not automatic current avatar.

The user can explicitly choose the generated/chat asset and copy it into avatar storage via `avatar/from-chat`.

ImageGen provider/config details are documented in [`VISUAL_GENERATION.md`](VISUAL_GENERATION.md).

## 7. Local ownership

Once selected, avatar state is copied under configured avatar directory (default derived from DB directory).

This means：

- current avatar does not point directly to a third-party CDN；
- deleting a source chat media later should not silently break selected avatar；
- sidebar/header/direct/group UI can all resolve the same local versioned avatar URL。

## 8. Boundaries

- Avatar is an asset, not a relationship score.
- Search result selection does not become long-term Memory by default.
- Search Provider is not exposed as arbitrary Character web browsing.
- Generated avatar does not auto-commit over current avatar.
- Current avatar reference can help SELFIE/AVATAR identity consistency, but SCENE generation does not need to force the person into every image.
- Camera/Screen Visual Capture is not an avatar source; it is transient Vision context for a current turn.
