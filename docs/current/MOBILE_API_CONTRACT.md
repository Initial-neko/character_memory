# Android / External Client API Contract (V1)

> Status: **source-level IMPLEMENTED vs PROPOSED inventory**, reviewed on 2026-10-02 against feature source based on Core base `bb1f637323c4b6fe01e6fd494ca33aac58694ccb`. The original audit referenced an unmerged feature branch; that historical caveat does not describe the present main branch. Every route's live availability must be verified against the current deployed OpenAPI, not inferred solely from this document. The machine-readable route inventory is source-checked; deployed runtime schemas remain authoritative. [Android consumer](https://github.com/Initial-neko/character_memory_android).

## 1. Scope and ownership

- **Core repository** owns HTTP paths, methods, request/response semantics, authentication, SSE event contracts and backward-compatibility rules.
- **Android repository** owns Compose UI, API clients, CameraX, AudioRecord, media playback, MediaProjection, permissions, Foreground Service, local preferences and client-side recovery.
- No mobile-specific PersonRuntime, duplicate SQLite source of truth, separate memory model or independent social scheduler.
- V1 features: direct chat, one-click character creation, one-click ensemble/group creation, groups, Space feed/comments, in-chat ImageGen, ASR/TTS call, camera keyframe chat, screen keyframe chat/observations, basic local preferences.
- Out of scope: Dev Console, Settings Center administration, provider secrets, TTS Workbench, Live2D/3D avatars, full on-device LLM and autonomous background recording.

### Transport — CURRENT

Use Tailscale **on both devices**; Tailscale is the private transport, **not** the application API or per-client authentication.

| Base | Tailscale Serve URL | Upstream | Purpose |
|---|---|---|---|
| `CORE` | `https://<node>.<tailnet>.ts.net` | `127.0.0.1:8000` | business HTTP + SSE + binary assets |
| `MEDIA` | `https://<node>.<tailnet>.ts.net:8443` | `127.0.0.1:8001` | ASR HTTP/WS + TTS binary |

Do **not** use the Android device's own `127.0.0.1:8000/8001`. The existing `scripts/mobile-start.sh` and `scripts/mobile-check.sh` set up and verify this routing. Settings `:8003`, Dev `:8002` and TTS Workbench `:9002` are deliberately PC-local. Do not expose them wholesale to make the App work. A future one-origin gateway is OPTIONAL; it does not exist in V1.

All paths below are relative to the noted base. JSON request/response is default **except** SSE, WAV upload, TTS audio and media assets. Exact runtime OpenAPI documents are `CORE/openapi.json` and `MEDIA/openapi.json`; consult those for full generated models. A source-checked, machine-readable **route subset inventory** is maintained at [docs/contracts/android-v1-route-inventory.json](../contracts/android-v1-route-inventory.json). It records 22 V1 paths and their source files, but does **not** replace generated OpenAPI or runtime schema tests.

## 2. API compatibility and error contract

- Do not rename/remove implemented `/v1/*` paths or required fields without a deliberate versioned migration. Preserve existing WebUI behavior.
- Any new response fields should be additive; clients must tolerate unknown JSON fields and optional fields. Persisted IDs are opaque strings except explicitly numeric event/post IDs.
- Errors commonly follow FastAPI `{"detail": ...}`; `detail` can be a string **or an object** (e.g., active-character soft-limit 409). Do not hardcode all errors as a string.
- Expect `400/404/409/413/415/422/502/503` depending on route; `429` is not currently a guaranteed unified rate-limit code.
- Endpoint acceptance is not model completion. `202 Accepted` means the user fact has been stored/enqueued; PersonRuntime can reply later, remain silent, or raise an asynchronous `reaction_error`.
- App must gracefully handle PC offline, lost tailnet route, application-layer 401/403 **once introduced**, and media-runtime outage independently of text chat.
- **Current security boundary** is private Tailnet + Serve. A per-device app bearer-token/permission mechanism is **PROPOSED**; do not present it as an existing route.

## 3. Implemented Core API inventory (CORE :443)

| Capability | Method + path | Request / response notes |
|---|---|---|
| Characters | `GET /v1/characters?archived=false&include_deferred=false` | `{characters,soft_limit,active_limit,active_total,overflow_count}`; Space mention pickers should request `include_deferred=true`, since non-archived deferred characters are valid Space participants but are hidden from the direct-chat sidebar |
| LLM Usage | `GET /v1/llm/usage?hours=1&limit=80` | existing read-only Core usage view; `hours` 1..2160 (default 24), `limit` 1..300 (default 80); `{window_hours,summary,by_feature,by_model,recent}`. Includes request/token/retry/error/latency attribution; no monetary cost field is available, so clients must not infer currency cost. Does not return Prompt/Response bodies. |
| Character life observer | `GET /v1/characters/{character_id}/life` | Read-only operator projection; `day=YYYY-MM-DD`, UTC `offset` minutes (-840..840), `channel=ALL/DIRECT/GROUP/SPACE/WORLD/INTENT`, `view=life/changes/relationships/intents`, literal `query` up to 200 characters, relationship `peer`, opaque `cursor`, `limit` 1..50 (default 30). Returns `{character_id,items,next_cursor,read_only,undated_count}`; each item has namespaced `key`, actual source, result, optional saved state comparison/Memory/Intent audit. No model calls, public feed or synthetic relationship scores. See MEMORY for privacy/provenance boundary. |
| Sidebar summaries | `GET /v1/characters/summaries` | `{characters:[...]}`; includes latest-message summary |
| Character draft | `POST /v1/characters/draft` | request: `description` required, optional `name,age,tags`; response `{draft}` |
| Confirm new character | `POST /v1/characters` | request: `{draft,character_id?,confirm_over_soft_limit?,creation?}`; response `{character,description}`; may return 409 |
| Persona detail | `GET /v1/characters/{character_id}/persona` | persona inspector payload |
| Open deferred direct | `POST /v1/characters/{character_id}/open-direct` | moves a group-created deferred character to direct list |
| Direct history | `GET /v1/chat/history-page?character_id=X&limit=50&before_id=N` | `{character_id,messages,has_more,next_before_id}`; paginate with returned cursor |
| Send direct | `POST /v1/chat/messages` | 202, `{accepted,event_id,message}`; details below |
| Group list | `GET /v1/groups?archived=false` | `{groups}` |
| Simple group create | `POST /v1/groups` | `{name,member_ids:[...>=2]}` → `{group}` |
| Group history | `GET /v1/groups/{conversation_id}/history?limit=50&before_id=N` | `{group,messages,has_more,next_before_id}` |
| Send group | `POST /v1/groups/{conversation_id}/messages` | 202, `{accepted,event_id,turn_id,message}` |
| Rename group | `PATCH /v1/groups/{conversation_id}` | `{name}` |
| Manage membership | `POST /v1/groups/{id}/members`; `DELETE /v1/groups/{id}/members/{character_id}` | existing membership API |
| Group archive | `POST /v1/groups/{id}/archive`; `POST /v1/groups/{id}/restore` | response `{group}` |
| AI ensemble prepare | `POST /v1/ensembles/prepare` | `{prompt: string(3..2000)}` → `{build}` |
| AI ensemble resume | `GET /v1/ensembles`; `GET /v1/ensembles/{group_id}` | in-progress / resumable build |
| AI ensemble retry | `POST /v1/ensembles/{group_id}/research`; `POST /v1/ensembles/{group_id}/members/{index}/retry` | resumable build on transient failure |
| AI ensemble confirm | `POST /v1/ensembles/{group_id}/confirm` | `{selected_indices:[2..12],confirm_over_soft_limit?:false,use_voice_design?:false}` → `{build}` |
| AI ensemble cancel | `POST /v1/ensembles/{group_id}/cancel` | `{build}` |
| Events | `GET /v1/events/stream` | SSE (see section 4) |
| Space list | `GET /v1/space/posts?limit=10&before_id=N&character_id=X` | `{posts,total,has_more,next_before_id,...}`; server caps page to 10 |
| Space post detail | `GET /v1/space/posts/{post_id}` | `{post}` |
| Space comment | `POST /v1/space/posts/{post_id}/comments` | `{content,reply_to_comment_id?,sticker_id?,mentions?:[character_id],client_request_id?}`; omit `character_id` for human user; user comments may mention up to 4 active characters by stable ID; a repeated request ID with identical payload returns the existing comment, while reuse with a different payload returns 409 |
| Space notifications | `GET /v1/space/notifications?unread_only=true&limit=50`; `POST /v1/space/notifications/{notification_id}/read` | durable in-app unread notifications for role replies to user comments and structured role @user; list cap 100; read is idempotent; no background OS push |
| Space reactions/views | `PUT/DELETE /v1/space/posts/{post_id}/likes/{character_id}`; `PUT /v1/space/posts/{post_id}/views/{character_id}` | existing character-scoped actions; not generic human like API |
| AI image rewrite | `POST /v1/characters/{id}/images/rewrite` | `{instruction,purpose?,provider?,use_avatar_reference?}` → `{prompt,...}` |
| AI image draft | `POST /v1/characters/{id}/images/generate` | same request + `persist_result?`; returns sendable `image.data_url` (not automatically a chat event) |
| Asset | `GET /v1/media/{media_id}` | image/audio binary; not JSON |
| Stickers | `GET /v1/stickers`; `GET /v1/stickers/{character_id}/{sticker_id}/asset` | `?character_id=` adds that role's private pool (`scope:"character"`); without it, the shared public pool. Render assets with the character-scoped route: a live stream event carries only `sticker_id`, and a private id 404s on the global route. |
| Sticker import | `POST /v1/stickers/import` | Raw ZIP (`application/zip`) or transparent 3×3 8-bit non-interlaced RGBA PNG (`image/png`, filename `.png`); validated atomically; `scope=global` (default, shared) or `scope=character` + `character_id` (that role's private pool); `auto_tag=false` avoids model calls. See CONVERSATION_RUNTIME for limits. |
| Avatar | `GET /v1/characters/{id}/avatar/asset` | binary; optional V1 client display |
| Visual config | `GET /v1/visual/periodic/config` | `{enabled,interval_seconds,max_per_hour,scope:"DIRECT_DISPLAY_ONLY"}` |
| Visual direct chat | `POST /v1/visual/direct/messages` | 202, text + bounded transient frames |
| Visual group chat | `POST /v1/visual/groups/{conversation_id}/messages` | 202, text + bounded transient frames |
| Direct screen observation | `POST /v1/visual/direct/observations` | 202, low-priority, only DISPLAY source; `{accepted,reason,...}` can reject without HTTP error |

Sticker library management (`DELETE /v1/stickers`, single ID or current pack) belongs to the PC local panel. The Android UI only reads the resulting active catalog and sends available stickers; it has no import/removal management entry. Removed stickers remain readable through historical message payloads and asset URLs. See [CONVERSATION_RUNTIME](CONVERSATION_RUNTIME.md#stickers) for persistence and removal semantics.

For the normal ImageGen flow, call `POST /v1/characters/{character_id}/images/generate` once: Core performs prompt rewriting and image generation in that request, then returns a sendable draft. Keep message sending as a separate explicit user action; do not require a standalone `/images/rewrite` click first.

### Direct message example — CURRENT

~~~http
POST /v1/chat/messages
Content-Type: application/json

{
  "character_id": "rin",
  "conversation_id": "a-stable-id-for-this-direct-session",
  "message": "你好"
}
~~~

Success: `202` `{"accepted":true,"event_id":123,"message":{...}}`. Direct request permits optional `sticker_id` or `image:{filename,data_url}` instead of text, but **sticker and image cannot be supplied together in the same turn**. The current image payload is a base64 data URL, not multipart.

Group message route shares the 202 enqueue semantics and supports optional `mentions` (up to 4) and image/sticker. Do not use the legacy synchronous `POST /v1/chat` or `POST /v1/groups/{id}/chat` as the V1 UI send path.

### Space mentions and in-app notifications — CURRENT

A user Space comment may include `mentions`, an array of at most four unique **active character IDs**. IDs are validated by Core and persisted with the comment; display names are never parsed to decide identity. Each explicitly mentioned role receives one normal `SPACE_COMMENT_RECEIVED` decision, even when autonomous thread reply rounds are configured as zero. If the same comment replies to a character comment, its reply target is dispatched first and de-duplicated against the explicit mentions; the reply target still follows the configured reply-round setting. A role may remain silent, and an explicit @ does not start an AI-to-AI reply chain. The comment POST remains an immediate durable write/queue response with `thread_replies: []`; a reply may arrive later. The follow-up decision queue is bounded and in-memory in this release: a Core restart or a full queue can leave the durable comment without its pending role decision. The comment and any notification already written remain durable; pending role decisions are not rebuilt after restart.

Example user comment request:

~~~json
{
  "content": "你们愿意一起去吗？",
  "reply_to_comment_id": 123,
  "mentions": ["rin", "mei"],
  "client_request_id": "c6d79c3e-b99d-4d57-a381-5369186a6d4b"
}
~~~

The response keeps the existing shape: `{comment,thread_replies,post}`. `comment` contains `id,post_id,character_id,actor_type,content,sticker_id,created_at,reply_to_comment_id,mentions,mentions_user,author,sticker`; a user-authored comment has `character_id:"user"`, `actor_type:"USER"` and `mentions_user:false`. `thread_replies` is currently an empty array because the scheduler runs after the request.

`client_request_id` is optional and limited to 64 characters. Repeating the same key with the same post, author, content, reply target, sticker and mention list returns the existing comment and does not enqueue the role decisions a second time. Reusing the key with a different payload returns `409`.

`GET /v1/space/notifications` defaults to unread items (`unread_only=true`) and 50 results (`limit`, maximum 100). Each notification contains `id`, `post_id`, `comment_id`, `reasons` (`REPLY`, `MENTION`, or both), `created_at`, `read_at`, plus the source comment and post preview. A role reply to a USER-authored comment creates `REPLY`; a role `SPACE_COMMENT` with `mentions_user=true` creates `MENTION`. These conditions aggregate into one notification per role comment. The `recipient_id` is currently the local singleton `user`; the service has no per-device credential or multi-user identity contract.

Representative response:

~~~json
{
  "notifications": [
    {
      "id": 7,
      "recipient_id": "user",
      "post_id": 42,
      "comment_id": 59,
      "reasons": ["REPLY", "MENTION"],
      "created_at": "2026-10-02T09:00:00+00:00",
      "read_at": null,
      "comment": {
        "id": 59,
        "post_id": 42,
        "character_id": "rin",
        "actor_type": "CHARACTER",
        "content": "我看到你喊我啦。",
        "sticker_id": null,
        "created_at": "2026-10-02T09:00:00+00:00",
        "reply_to_comment_id": 51,
        "mentions": [],
        "mentions_user": true,
        "author": {"id":"rin","name":"Rin","identity":"","tagline":"","avatar_url":"","archived":false},
        "sticker": null
      },
      "post": {
        "id": 42,
        "character_id": "rin",
        "author": {"id":"rin","name":"Rin","identity":"","tagline":"","avatar_url":"","archived":false},
        "content": "今天去看海。",
        "created_at": "2026-10-02T08:50:00+00:00"
      }
    }
  ],
  "unread_count": 1,
  "max_items": 100,
  "background_push": false
}
~~~

`POST /v1/space/notifications/{notification_id}/read` returns `{notification,unread_count}` and is idempotent: repeated reads preserve the first `read_at`. Web and Android should poll the unread count only while their foreground UI is active, show an in-app badge, and mark a notification read when the user opens it. The endpoint does not send Android system notifications or provide background push delivery.

## 4. SSE event contract — CURRENT

~~~http
GET /v1/events/stream?scope=direct&character_id=rin&conversation_id=a-stable-id-for-this-direct-session
Accept: text/event-stream
~~~

For group use `scope=group&conversation_id=<group_id>`, no `character_id` required. Server returns named SSE event types:

| Event name | Client behavior |
|---|---|
| `reaction_status` | reconcile queued/typing/idle/superseded state; not proof of message persistence |
| `character_event` | direct character message, dedupe against durable history |
| `group_character_event` | group character message |
| `group_member_complete` | group turn progress/silent-member summary |
| `reaction_error` | show a non-destructive reaction failure; user fact is already saved |

The server supports the HTTP `Last-Event-ID` header for transient SSE resume. **It is NOT a durable cross-restart event log**. On first connect, reconnect, app resume or scope change, GET the durable history page and merge/dedupe by persisted message/event IDs. Do not assume every response must generate a model message; `actions=[]` (silence) is valid. Manage stream lifecycle separately for direct and group.

**Intended separation (2026-10-08):** Web `web/app.js` and Android use client-local Direct `conversation_id` choices. Cross-device history merging, canonical identity and shared read cursors are **not planned**. Each client must scope its own SSE subscription, durable-history reconciliation and duplicate suppression to its own conversation ID; no Web ↔ Android shared Direct session is promised.

### VOICE_MESSAGE payload fields — existing Core behavior

Core persists a `VOICE_MESSAGE` as a text-bearing message before synthesis. Its metadata starts as `voice_status:"pending"`, `voice_media_id:null`, `voice_duration_ms:null`, and `voice_error:null`. Materialization updates the same persisted event to `ready` with `voice_media_id` and optional `voice_duration_ms`, or to `failed` with `voice_error`; the text remains available when synthesis fails. The canonical source keys are defined in `voice_message_fields.py`.

Direct and Group history payloads expose these four values as top-level message fields (null for non-voice messages). Direct `character_event` and Group `group_character_event` SSE payloads carry them inside `metadata`; the Web projection promotes them to the same top-level shape and merges pending/ready/failed updates by the original event ID. A client should preserve the event ID and map the nested SSE metadata when updating its message projection. When `voice_status` is `ready`, fetch the audio binary through Core `GET /v1/media/{voice_media_id}`; this does not add a `voice_url` field or a new route.

This documents existing Core source behavior for future Android integration. Android recording and playback are not implemented by this Core feature branch.

## 5. Media API (MEDIA :8443) — CURRENT

| Method + path | Media type | Usage |
|---|---|---|
| `GET /health` | JSON | service probe |
| `POST /v1/asr` | request: raw `audio/wav` PCM16; response: JSON | ASR; max request bytes defaults to 4 MiB; `X-ASR-Source: call` optional |
| `WS /v1/asr/stream` | WebSocket, custom messages | streaming ASR exists; defer until native batch ASR is stable |
| `POST /v1/tts` | request: JSON; response: WAV/other audio binary | `{text,voice?,speaker_id?,speed?}`; text 1..4000 characters |

Native capture/playback are **not API capabilities**. Android needs AudioRecord and output/audio focus. Do not parse `/v1/tts` as JSON. Do not call PC loopback `:9002` directly; the formal provider selection happens behind Media Runtime.

The existing Web `voice.js` performs ASR → chat/visual message 202 → SSE reaction → TTS as a **client orchestration state machine**. A native Android call controller must preserve turn ordering, listening-vs-speaking state, deferred input, cancel/end cleanup and no duplicate playback.

## 6. Vision keyframe API — CURRENT

~~~json
{
  "character_id": "rin",
  "conversation_id": "a-stable-id-for-this-direct-session",
  "message": "看看这个画面",
  "visual_frames": [
    {
      "filename": "frame.jpg",
      "source": "CAMERA",
      "captured_at_ms": 12345678,
      "data_url": "data:image/jpeg;base64,<encoded-frame>"
    }
  ]
}
~~~

- `source` is `CAMERA` or `DISPLAY`.
- `visual_frames` length 1..5; one frame <=2 MiB decoded, combined <=6 MiB; supported image encodings are enforced server-side in `normalize_visual_frames`.
- Raw keyframe bytes are **transient turn context**; they are not persisted as chat attachments. Only summaries/metadata are durable.
- Group visual chat accepts `message`, `visual_frames` and optional `mentions`, with the group ID in the path.
- Periodic direct observation accepts **one DISPLAY frame** as `visual_frame` and may reply `{accepted:false,reason:"DISABLED"|"BUSY"|"DUPLICATE_FRAME"|"INTERVAL"|"HOURLY_LIMIT"|...}`. Only direct display observations are supported; **camera periodic observation and group periodic observation are not currently promised**.
- Android CameraX / MediaProjection own the actual capture; they should sample, detect changes, encode, bound buffering and obtain explicit OS screen-recording consent. A POST endpoint **cannot** acquire device screens.

### RSS external information — implemented in source

Android consumes Core-owned RSS data and collection through the CORE origin. See [RSS Sources](RSS_SOURCES.md) for the complete query and sanitized-content contract; deployment OpenAPI must be checked before integration. GET/POST `/v1/rss/sources`, PATCH/DELETE `/v1/rss/sources/{id}`, POST `/v1/rss/sources/{id}/restore`, POST `/v1/rss/sources/{id}/refresh`, GET `/v1/rss/categories`, GET `/v1/rss/items`, GET `/v1/rss/items/{id}` and GET `/v1/rss/items/{id}/image?url=...` are the client surface. Source GET accepts `include_cancelled=true` for recovery UI. Cancel retains historical articles; restore reuses the same source and immediately fetches. Add/restore already perform the immediate fetch and return `{source,refresh}`; failed fetch preserves subscription. Clients must never run a second grabber, copy the facts database, or automatically replay subscription writes. Android image requests pass the original recorded URL through the Core image endpoint. Characters do not automatically consume these items.

## 7. Optional/future contracts (PROPOSED or explicitly not planned)

| Proposed capability | Suggested contract | Blocking? |
|---|---|---|
| Pair / revoke native device | one-time pair request + scoped device credential + revoke / expiry | **Required before exposing native privileged calls**; not currently callable |
| Canonical direct conversation ID | **NOT PLANNED:** no unified Web/Android Direct ID or legacy Web migration | out of scope by user decision (2026-10-08) |
| Cross-device read cursor | **NOT PLANNED:** no synchronized cross-device unread cursor | out of scope; local client notification state may still exist |
| Device capability / diagnostics | app version, last seen, mic/camera/screen capability, operational status | after basic end-to-end connectivity |
| Background notification delivery | explicit push transport and opt-in; do not assume SSE is Android background push | phased enhancement, not existing |
| Multipart media upload | bounded binary upload + asset ID accepted by message API | optional optimization; current data URL is already callable |
| Native capture session binding | explicit target ownership and revocation if capture continues while Web UI changes target | required for cross-app/background screen capture if not bound by active call |

**Names/paths above are intentionally NOT frozen.** Author Core RFC + tests before declaring a new endpoint `CURRENT`. Device tokens must be scoped, stored using Android Keystore, revocable and unable to call PC-local Dev/Settings/Secret APIs. Tailnet membership alone does not define product roles. Existing Web clients must remain functional when optional security contracts are implemented; no cross-client Direct identity migration is planned.

## 8. Change process and integration acceptance

1. Core PR: edit this contract and runtime implementation together; add route/schema validation and fixture tests; keep current WebUI working.
2. Android PR: consume a pinned documented Core contract revision; add mock HTTP/SSE fixtures and JVM/UI tests; never read/write Core SQLite.
3. Cross-repository integration: real Core (character + media) on PC, phone on Tailnet, first-person chat → 202 → SSE → history reconcile, AI ensemble, Space, ImageGen and native media.
4. CI: distinguish static contract tests, mock emulator tests and actual Android hardware validation. Simulator cannot establish actual microphone/camera/MediaProjection reliability.
5. No unilateral breaking API change. When fields/stream semantics change, update source code, this Core document, Android contract fixtures and both repositories' release compatibility notes in the same work item.

## 9. Code references

- Routing and schemas: `src/character_memory/api_contracts.py`, `core_character_web.py`, `async_web.py`, `group_web.py`, `ensemble_web.py`, `space_web.py`, `visual_capture_web.py`, `visual_web.py`, `core_resource_web.py`, `media_server.py`.
- Web consumers: `web/app.js`, `web/groups.js`, `web/ensemble.js`, `web/space.js`, `web/voice.js`, `web/visual_capture.js`, `web/mobile_access.js`, `web/unread.js`.
- Deployment: [MOBILE_ACCESS.md](MOBILE_ACCESS.md), [ARCHITECTURE.md](ARCHITECTURE.md), [CONVERSATION_RUNTIME.md](CONVERSATION_RUNTIME.md), [VISUAL.md](VISUAL.md), [VOICE_AND_TTS.md](VOICE_AND_TTS.md).

World local recovery: GET `/v1/world/activity/recovery` returns a bounded read-only eligibility report. POST `/v1/world/activity/recovery/{opportunity_id}` commits an existing complete APPRAISED receipt without provider calls; unknown opportunities return 404 and unsafe/incomplete receipts return 409. This is a local operator boundary, not an autonomous action granted to a character.

Sticker PNG import accepts optional query `normalize_background=false`; enabling it explicitly applies exact solid-color border-connected background removal before strict 3×3 validation. It is independent of `auto_tag`, introduces no Vision call, and is ignored for ZIP imports. Complex/nonuniform backgrounds remain unsupported.
