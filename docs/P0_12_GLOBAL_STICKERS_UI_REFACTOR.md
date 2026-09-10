# P0.12 — Global Stickers + Conversation UI Refactor

## Why

P0.11 exposed two structural problems rather than isolated UI bugs:

1. Direct chat and group chat both installed composer `submit` listeners. The older direct-chat listener ran first in capture phase and called `stopImmediatePropagation()`, so a message typed while the UI showed a group could still be sent through `/v1/chat`.
2. P0.5–P0.11 frontend features were layered by replacing global functions from later scripts. Sticker rendering, composer layout and pack switching had already regressed because the last loaded script silently won.
3. Web-imported stickers were stored under one persona directory, so a pack imported while chatting with one character disappeared after switching to another character. That does not match a WeChat-like user sticker library.
4. Group routes owned a second lazy runtime/model bundle instead of sharing the application runtime used by direct chat.

## Decisions

### One conversation send owner

`web/app.js` owns the only composer submit handler. It routes by explicit conversation state:

- `DIRECT` -> `/v1/chat`
- `GROUP` -> `groups.sendText()` -> `/v1/groups/{conversation_id}/chat`

Sticker and image modules make the same routing decision. Feature modules do not install competing submit handlers and do not replace core functions.

### Responsibility-based Web modules

The runtime JS set is now:

- `app.js` — state, API, message renderer, direct chat, one submit router
- `persona.js` — character builder
- `unread.js` — direct-chat unread summaries
- `stickers.js` — global sticker picker/import/send
- `images.js` — image picker, clipboard input and send
- `groups.js` — group list/create/history/send/turn inspector
- `intent.js` — direct-chat Intent inspector

The old `p0_5.js`, `p0_6.js`, `p0_7.js`, `p0_8.js`, and `p0_11.js` override chain is removed. Historical CSS remains temporarily because it is declarative and does not own runtime behavior; it can be consolidated separately without mixing a visual rewrite into this correctness refactor.

### Global user sticker library

Web imports now write to `Settings.sticker_dir` (default `<db parent>/stickers`) rather than `personas/<character>/stickers`.

The effective catalog merges, in order:

1. built-in default stickers;
2. legacy character-local manifests, for migration compatibility;
3. the global user manifest, which wins on duplicate IDs.

All loaded character runtimes receive the same effective StickerCatalog. Therefore a user-imported pack is available after switching characters and in group chat. Existing character-local imports remain visible without requiring an immediate file migration.

### Shared application runtime

`create_api()` exposes an internal application access object through `app.state.character_memory`. Group routes reuse that same lazy AppBundle, model, embeddings, ChatService locks, media storage and SQLite lifecycle. `group_web.py` no longer constructs or closes a second GroupRuntimeManager.

### Group user stickers

A global sticker can now be sent by the user in a group. It is persisted once as a shared ConversationEvent with semantic sticker metadata. It is not copied into any character's direct Event Log. Every participating character can independently react or stay silent.

## Non-goals

- No migration of direct-chat Event tables into the new Conversation tables yet.
- No group proactive Intent yet.
- No full CSS redesign in this change.
- No arbitrary file attachments; image input remains JPEG/PNG/GIF/WebP.

## Regression gates

Tests enforce that:

- only `app.js` owns composer submit;
- group feature code cannot install another submit handler;
- the old version override scripts are absent;
- group text/image/sticker sends route through group APIs;
- group sticker input remains one shared fact;
- Web sticker import updates all loaded runtimes and returns the same global catalog across characters;
- tagged ZIPs do not incur Vision calls, while image-only ZIPs can use AI auto-tagging.
