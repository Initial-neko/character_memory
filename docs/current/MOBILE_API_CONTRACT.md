# Android / External Client API Contract (V1)

> Status: **IMPLEMENTED vs PROPOSED inventory**, verified against the Character Memory Core `main` source on 2026-10-01. This is the **Core-owned, human-readable client contract**. An entry marked PROPOSED is **not callable yet**. The FastAPI runtime schemas and actual route code remain authoritative for implementation details. [Android consumer](https://github.com/Initial-neko/character_memory_android).

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

All paths below are relative to the noted base. JSON request/response is default **except** SSE, WAV upload, TTS audio and media assets. Exact runtime OpenAPI documents are `CORE/openapi.json` and `MEDIA/openapi.json`; consult those for full generated models.

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
| Characters | `GET /v1/characters?archived=false&include_deferred=false` | `{characters,soft_limit,active_limit,active_total,overflow_count}` |
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
| Space comment | `POST /v1/space/posts/{post_id}/comments` | `{content,reply_to_comment_id?,sticker_id?}`; omit `character_id` for human user |
| Space reactions/views | `PUT/DELETE /v1/space/posts/{post_id}/likes/{character_id}`; `PUT /v1/space/posts/{post_id}/views/{character_id}` | existing character-scoped actions; not generic human like API |
| AI image rewrite | `POST /v1/characters/{id}/images/rewrite` | `{instruction,purpose?,provider?,use_avatar_reference?}` → `{prompt,...}` |
| AI image draft | `POST /v1/characters/{id}/images/generate` | same request + `persist_result?`; returns sendable `image.data_url` (not automatically a chat event) |
| Asset | `GET /v1/media/{media_id}` | image/audio binary; not JSON |
| Stickers | `GET /v1/stickers`; `GET /v1/stickers/{sticker_id}/asset` | JSON catalog, binary asset |
| Avatar | `GET /v1/characters/{id}/avatar/asset` | binary; optional V1 client display |
| Visual config | `GET /v1/visual/periodic/config` | `{enabled,interval_seconds,max_per_hour,scope:"DIRECT_DISPLAY_ONLY"}` |
| Visual direct chat | `POST /v1/visual/direct/messages` | 202, text + bounded transient frames |
| Visual group chat | `POST /v1/visual/groups/{conversation_id}/messages` | 202, text + bounded transient frames |
| Direct screen observation | `POST /v1/visual/direct/observations` | 202, low-priority, only DISPLAY source; `{accepted,reason,...}` can reject without HTTP error |

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

**Current cross-client gap:** Web `web/app.js` creates a per-character `conversation_id` in browser `localStorage`. Android must not independently invent a conflicting ID and assume perfect cross-device SSE routing. A Core-owned migration/identity contract is proposed in section 7; before it lands, the Android consumer must implement and test a deliberate compatibility strategy.

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

## 7. Missing contracts (PROPOSED, not implemented)

| Proposed capability | Suggested contract | Blocking? |
|---|---|---|
| Pair / revoke native device | one-time pair request + scoped device credential + revoke / expiry | **Required before exposing native privileged calls**; not currently callable |
| Canonical direct conversation ID | server-owned get-or-create or migration mapping; preserve old Web IDs | **Required for trustworthy Web ↔ Android live consistency** |
| Cross-device read cursor | per-user/per-conversation last-read durable cursor | optional initial UI; required before claiming synchronized unread |
| Device capability / diagnostics | app version, last seen, mic/camera/screen capability, operational status | after basic end-to-end connectivity |
| Background notification delivery | explicit push transport and opt-in; do not assume SSE is Android background push | phased enhancement, not existing |
| Multipart media upload | bounded binary upload + asset ID accepted by message API | optional optimization; current data URL is already callable |
| Native capture session binding | explicit target ownership and revocation if capture continues while Web UI changes target | required for cross-app/background screen capture if not bound by active call |

**Names/paths above are intentionally NOT frozen.** Author Core RFC + tests before declaring a new endpoint `CURRENT`. Device tokens must be scoped, stored using Android Keystore, revocable and unable to call PC-local Dev/Settings/Secret APIs. Tailnet membership alone does not define product roles. Existing Web clients must remain functional during migration.

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
