# Changelog

This file records user-visible and architecture-significant release changes. Historical P0.x milestone notes remain under `docs/archive/`.

## 0.5.0 - 2026-10-16

First **stable baseline** of the Persistent Person Runtime, promoting the `v0.5.0-rc.1` line after 218 commits of integration work.

The notes below record the delta **since the release candidate**; the `0.5.0-rc.1` entry further down still describes the capabilities that line introduced. No manual data migration is required: every schema addition since the RC is an additive, idempotent `apply_schema_migration` applied at startup, and historical records stay readable.

### World perception: RSS and sourced observation

- RSS external sources V1 subscribe, cancel and restore without losing retained history, with today/title-query feeds, per-subscription scoping, preserved article paragraphs and inline images, and the original link attributed below the content.
- What a character read is now a **recorded observation** rather than an imagined one: bounded personal RSS reading, sourced historical observations that are recallable, a sourced character-life observation page showing the reading facts and usage behind them, and prompt handling that distinguishes a sourced experience from an imagined history.
- Chinese reading titles are recalled with their scoped source context.
- World Activity gained a bounded capability-execution contract: character capability grants now gate whether research or Skill mounting is available at all, a plan executes through one explicit path, and each run records bounded tool-step receipts.
- Claude world execution runs through a bounded stateless print backend with isolated tool sessions and an owned subprocess lifecycle, so a World run cannot outlive its parent or leave a process group behind.
- Optional per-character vocation focus narrows personal browsing, and opt-in DeepSeek reference-window pacing bounds how often browse planning repeats.

### Direct and Group

- Opt-in fresh Claude task delegation in Direct lets a character hand work to a fresh isolated run instead of only talking about it.
- Bounded opt-in Direct call invitations, with storage that stays lazy while the capability is off, and bounded opt-in autonomous Direct images.
- User-controlled warm fantasy scenes can be previewed, created as drafts with fresh characters, entered and resumed.
- Group participation is provenance-backed and cost-aware: shared history is retrieved from actual recent input receipts rather than reconstructed, and bounded counterpart relationship review uses explicit group input provenance.
- A proactive share is grounded in bounded sourced observations, and social intents no longer leak into the Direct proactive dispatch path.
- Autonomous group conversations stay out of the user's inbox.

### Frontend

- The **approved frontend visual system** is implemented, with generated artwork integrated and media presentation made consistent across surfaces.
- Settings gained schema-driven category navigation with a mobile strip.
- Live2D reached the call stage: an immersive stage with a persistent voice session, automatic motion and expression, plus model import, replacement and unbinding.
- Usage-first mobile flows and Space notifications; the Dev Console prioritizes one-hour LLM usage and marks monetary cost as unknown rather than inventing a number.
- Sticker pools are isolated per character instead of one global selection, transparent nine-grid PNG import goes through the global catalog, and removal-mode UI plus Windows atomic replace were repaired.

### Media, storage and correctness

- **Security:** RSS and media fetches pin their connection to a validated public IP, so a hostname that passed validation cannot afterwards be redirected to a private address (DNS rebinding / SSRF). RSS XML and URL handling is hardened and its shutdown is fenced.
- **Storage:** SQLite runs in WAL with bounded writer waits, a blocked event insert rolls back instead of leaking an open transaction, and the Core transaction connection is shared with the lazy runtime.
- **Memory:** re-embedding is atomic and Unicode deduplication is aligned, so recall cannot observe a half-migrated embedding set; invalid legacy event metadata is tolerated instead of breaking recall.
- Runtime workers roll back after a startup failure rather than staying half-registered; Direct SSE updates render incrementally instead of redrawing; idle conversation runtime caches are reclaimed safely.
- Settings: the runtime model is authoritative for validation bounds, typed consumers no longer carry silent defaults, and persistence is serialized against runtime apply.
- `character-stack` gained `--stop` / `--status` with launcher scripts, so the stack shuts its children down in reverse order and reports state without starting anything.
- The Android / external client V1 contract is a checked machine-readable route inventory rather than prose.
- Character Space comment reply rounds are configurable, and its mixed feed projects sourced RSS and topics alongside autonomous posts.
- Web search gained a keyless local provider for public-web discovery.

### Configuration and dependencies

- `config.example.yaml` grew by 114 lines covering the new settings; every capability added above is off or bounded by default.
- New hard dependency `webless==0.1.0` for the local public-web discovery provider.
- `Pillow==12.3.0` added to the `api` extra, and the wheel now ships `web/*.png` assets.

### Known limitations

- This line still carries the deferred debt recorded in [docs/current/TECH_DEBT.md](docs/current/TECH_DEBT.md), including the unpinned dependency lock — CI re-resolves the declared graph on every run.
- Acceptance beyond CI (target-machine ASR, screen readability, the real-Chromium visual pass) is recorded with the acceptance Issue rather than restated in this file.

## 0.5.0-rc.1 - 2026-10-01

First release candidate for the **Persistent Person Runtime Baseline**.

### Runtime

- Converged Direct and Group reaction evaluation onto a shared reaction engine while retaining channel-specific policies and persistence.
- Centralized expressive Action -> Message materialization for text, emoji, sticker, image, and voice messages.
- Centralized Direct/Group incoming user fact normalization so sticker/image semantics and media metadata cannot drift by channel. A pure-sticker user turn now carries `action="STICKER"` in message payloads where it used to carry no action; clients that branch on `action` for user turns should read it instead of assuming sticker turns have none.
- The core API reports the installed package version instead of a hardcoded string that had drifted from `pyproject.toml`.
- Centralized Direct/Group message projection and shared browser message-content rendering.
- Removed Group ImageGen runtime monkey-patching and made user-triggered Group visual generation an explicit runtime path.
- Preserved Direct Intent/Wake/Proactive semantics and Group ordering/mention/privacy/autonomy semantics as intentional policy differences.
- Every background worker now has one lifecycle owner, so schedulers stop and restart as a unit instead of each keeping its own thread discipline.
- Every feature handle is declared on `CharacterRuntimeAccess`; features no longer reach around the access object to runtime internals.
- Direct retains provider failure feedback and renders generated media live instead of waiting for a history refresh. Group keeps its reaction failures through terminal redraws and preserves autonomous turn ordering when a turn fails.

### Group Conversation

- Group chat now renders by **turn**, not by persisted event. Consecutive character events sharing `(turn_id, actor_id)` fold into a single ChatTurn: one bubble, one speaker name, text lines merged, and sticker/image/voice rendered inside that same row. Different characters never merge. Direct chat keeps its finer-grained continuous messages unchanged.
- Added `group_max_speakers_per_turn` (default **5**, range 1–12, Settings Center → Group Conversation). One user turn asks at most this many distinct members to react; explicitly @-mentioned members always participate, and unmentioned members are truncated from the tail of the rotating order so the audience still rotates across turns. Set it to 12 to restore the every-member-is-asked behavior. `reaction_complete` events now report `deferred_speaker_ids` and `max_speakers`. Persistence, event ids, `turn_id`, traces and Memory structure are unchanged.
- Members can be removed from an existing group, and ensemble-created characters keep their own interaction styles rather than converging on one voice.

### Media, Voice and Vision

- Added opt-in **short-video generation** to Character Space through the same media contract as image search, ImageGen and voice. It is **disabled by default**, requires `METASO_MINIMAX_API_KEY`, and is bounded by a durable account-wide daily CNY budget that is reserved *before* the provider task is created. This is the first paid media path, so the enable switch, resolution, duration ceiling and budget are all explicit settings.
- Streaming ASR is implemented end to end: a shared AudioWorklet client, migrated dictation, and migrated voice calls, each with a batch fallback. The canonical media setup deliberately stays on the **batch-compatible baseline** rather than requiring the streaming model set.
- Added bounded **periodic screen observation** during a direct voice call. It fires only while the user has explicitly shared a display, and is excluded from camera and group-call autoplay; browser-side change detection makes an opportunity cheap while the server interval and hourly ceiling remain the authoritative cost guards.
- Remote image downloads enforce the byte ceiling while the body streams, so a chunked response without `Content-Length` can no longer be buffered whole before the limit is consulted.

### Character Space and World

- One-prompt ensemble groups: build a whole group from a single prompt through `POST /v1/ensembles` -> `/research` -> `/confirm`, with candidates researched from the web and created once the user confirms them.
- Character capacity raised to a soft threshold of 10 and a hard ceiling of 20; the character list no longer truncates, and the ceiling is enforced by the API rather than by what the UI happens to show.
- Group member ceiling raised from 4 to 12. Note the cost shape: a user message is answered by every member, so a full group costs up to 12 model calls per turn, while an autonomous opportunity stays capped at 4.
- Characters can be archived, deleted, and given a deferred direct-chat state; onboarding creates default assets and the persona inspector is read-only.
- Character Space feed pages through older posts with the existing cursor instead of stopping at the first 10, and media open in an in-app lightbox.
- Character Space now proactively prefetches older cursor pages before the reader reaches the bottom instead of making downward scrolling expose the loading boundary.
- Autonomous Space audience fanout stays sparse: warm relationships are ranked first and only a couple of cold exploration slots are spent per post, so the ceiling does not become the target.
- Avatar generation can return a 1~4 image candidate batch (4 by default) with bounded style presets, reusing the explicit Image prompt-polish path before ImageGen.
- World Activity v3 splits shared Pulse aggregation from independent personal browsing, with a daily browse cap. Repeated browse planning is skipped while its signal is stale, the gate is keyed to a durable signal watermark, and group discussion reopens browse planning.

### Product baseline

- Persistent Persona, Memory, Mental State, Intent and Runtime Trace.
- Direct and Group chat with async durable acceptance and SSE reconciliation.
- Character Space with autonomous posts, audience reactions, search/world observation, image, voice and opt-in video media.
- Voice Message V1 across Direct and Group.
- Vision, transient Camera/Display capture, ImageGen, and bounded periodic screen observation.
- Settings Center, Dev Console, Media Runtime, TTS Provider Runtime/Workbench, and GSV voice-template flow.
- The Dev Console can force one explicit media intent — including a video — without moving the real scheduler, so every media capability has a deterministic acceptance path. Video is the only paid option there and its proxy timeout follows the video budget rather than the image one.

### Engineering

- Split the former core `api.py` route bucket into explicit Character, Resource, and Direct HTTP modules with typed route dependencies; `api.py` is now the composition/lifecycle root.
- Full Linux pytest, Windows-sensitive smoke, and browser smoke are release gates. The browser contract for the Dev Console runs in the browser-smoke job rather than in the default one.
- Configuration and Settings UI help became a tested contract: every schema field and secret must carry a level and non-empty help text, CI rejects fields without it, and `config.example.yaml` is checked against the schema.
- Documentation impact is declared on every PR through `Docs-Impact` / `Docs-Reason` / `Docs-Contracts`, and the maintained docs are checked for link and structure integrity.
- Storage boundaries were restored so conversation schedulers query through repositories instead of embedding SQL, and memory admission rechecks duplicates inside the commit transaction rather than only before it.
- Retired no-op configuration and redundant media guards rather than keeping dead knobs loadable forever; legacy `space_daily_window_*` keys still load but are no longer part of the settings schema.
- Started FastAPI lifecycle-warning cleanup by replacing the deprecated `@app.on_event` registration path with centralized lifecycle registration.
- Added an explicit release/version policy; stable baselines are immutable tags/releases rather than a moving stable branch.
