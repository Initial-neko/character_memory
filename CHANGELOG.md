# Changelog

This file records user-visible and architecture-significant release changes. Historical P0.x milestone notes remain under `docs/archive/`.

## Unreleased

### Group Conversation

- Group chat now renders by **turn**, not by persisted event. Consecutive character events sharing `(turn_id, actor_id)` fold into a single ChatTurn: one bubble, one speaker name, text lines merged, and sticker/image/voice rendered inside that same row. Different characters never merge. Direct chat keeps its finer-grained continuous messages unchanged.
- Added `group_max_speakers_per_turn` (default **5**, range 1–12, Settings Center → Group Conversation). One user turn asks at most this many distinct members to react; explicitly @-mentioned members always participate, and unmentioned members are truncated from the tail of the rotating order so the audience still rotates across turns. Set it to 12 to restore the every-member-is-asked behavior. `reaction_complete` events now report `deferred_speaker_ids` and `max_speakers`. Persistence, event ids, `turn_id`, traces and Memory structure are unchanged.

## 0.5.0-rc.1 - 2026-09-22

First release candidate for the **Persistent Person Runtime Baseline**.

### Runtime

- Converged Direct and Group reaction evaluation onto a shared reaction engine while retaining channel-specific policies and persistence.
- Centralized expressive Action -> Message materialization for text, emoji, sticker, image, and voice messages.
- Centralized Direct/Group incoming user fact normalization so sticker/image semantics and media metadata cannot drift by channel. A pure-sticker user turn now carries `action="STICKER"` in message payloads where it used to carry no action; clients that branch on `action` for user turns should read it instead of assuming sticker turns have none.
- The core API reports the installed package version instead of a hardcoded string that had drifted from `pyproject.toml`.
- Centralized Direct/Group message projection and shared browser message-content rendering.
- Removed Group ImageGen runtime monkey-patching and made user-triggered Group visual generation an explicit runtime path.
- Preserved Direct Intent/Wake/Proactive semantics and Group ordering/mention/privacy/autonomy semantics as intentional policy differences.

### Product baseline

- One-prompt ensemble groups: build a whole group from a single prompt through `POST /v1/ensembles` -> `/research` -> `/confirm`, with candidates researched from the web and created once the user confirms them.
- Character capacity raised to a soft threshold of 10 and a hard ceiling of 20; the character list no longer truncates, and the ceiling is enforced by the API rather than by what the UI happens to show.
- Group member ceiling raised from 4 to 12. Note the cost shape: a user message is answered by every member, so a full group costs up to 12 model calls per turn, while an autonomous opportunity stays capped at 4.
- Character Space feed pages through older posts with the existing cursor instead of stopping at the first 10, and media open in an in-app lightbox.
- Character Space now proactively prefetches older cursor pages before the reader reaches the bottom instead of making downward scrolling expose the loading boundary.
- Avatar generation can return a 1~4 image candidate batch (4 by default) with bounded style presets, reusing the explicit Image prompt-polish path before ImageGen.
- Persistent Persona, Memory, Mental State, Intent and Runtime Trace.
- Direct and Group chat with async durable acceptance and SSE reconciliation.
- Character Space with autonomous posts, audience reactions, search/world observation, image and voice media.
- Voice Message V1 across Direct and Group.
- Vision, transient Camera/Display capture, and ImageGen.
- Settings Center, Dev Console, Media Runtime, TTS Provider Runtime/Workbench, and GSV voice-template flow.

### Engineering

- Split the former core `api.py` route bucket into explicit Character, Resource, and Direct HTTP modules with typed route dependencies; `api.py` is now the composition/lifecycle root.
- Full Linux pytest, Windows-sensitive smoke, and browser smoke are release gates.
- Started FastAPI lifecycle-warning cleanup by replacing the deprecated `@app.on_event` registration path with centralized lifecycle registration.
- Added an explicit release/version policy; stable baselines are immutable tags/releases rather than a moving stable branch.
