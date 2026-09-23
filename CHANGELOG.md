# Changelog

This file records user-visible and architecture-significant release changes. Historical P0.x milestone notes remain under `docs/archive/`.

## Unreleased

Current `main` has moved beyond the `0.5.0-rc.1` artifact. Open PRs are not listed here until merged; implementation status lives in `docs/current/STATUS.md`.

### Product

- Added Random Encounter as a temporary candidate pool with WEB/GENERATED discovery, short-lived candidate chat, scheduler, and accept/dismiss lifecycle before a candidate becomes a formal Character.
- Added World Activity with independent World Pulse refresh/discussion and per-character Personal Browse clocks, explicitly decoupled from Character Space publishing and long-term Memory admission.
- Added bounded threaded Character Space replies with reply-to-reply interaction while keeping the visual thread shallow.
- Prefetches older Character Space cursor pages and supports generated avatar candidate batches with style presets.
- Reworked Character onboarding so new Characters persist creation provenance, receive a guaranteed first avatar through ImageGen -> web search -> local fallback, select an existing voice template when available, and expose the persisted base Persona through a read-only inspector.
- Ensemble-created Characters now converge on the same onboarding lifecycle instead of owning a parallel creation path.

### Runtime / correctness

- Preserves the model's own Space plan output for diagnostics when structured planning resolves to an empty action.
- Browser World fetching skips candidate URLs rejected by the public-network guard instead of aborting the whole batch.
- Decoupled call microphone capture from screen sharing and hardened browser capture ownership against late permission grants; a retired/hung-up call can no longer adopt a microphone stream that arrives after it is no longer wanted.
- Rebuilt archived-character voice lifecycle so archived Characters leave active voice/GSV/proactive paths while retaining reversible voice mappings.

### UI / developer surfaces

- Added hierarchical common/advanced/diagnostic levels to Settings Center and Dev Console so the first screen stays focused.
- Moved raw JSON diagnostic payloads behind explicit disclosure controls.
- Fixed chat bubble/sticker/caption consistency, contrast/focus states and small-button hit areas.
- Repaired the one-prompt AI group creation lifecycle on the current runtime baseline.

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
