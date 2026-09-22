# Changelog

This file records user-visible and architecture-significant release changes. Historical P0.x milestone notes remain under `docs/archive/`.

## Unreleased

No queued release changes yet.

## 0.5.0-rc.1 - 2026-09-22

First release candidate for the **Persistent Person Runtime Baseline**.

### Runtime

- Converged Direct and Group reaction evaluation onto a shared reaction engine while retaining channel-specific policies and persistence.
- Centralized expressive Action -> Message materialization for text, emoji, sticker, image, and voice messages.
- Centralized Direct/Group incoming user fact normalization so sticker/image semantics and media metadata cannot drift by channel.
- Centralized Direct/Group message projection and shared browser message-content rendering.
- Removed Group ImageGen runtime monkey-patching and made user-triggered Group visual generation an explicit runtime path.
- Preserved Direct Intent/Wake/Proactive semantics and Group ordering/mention/privacy/autonomy semantics as intentional policy differences.

### Product baseline

- Persistent Persona, Memory, Mental State, Intent and Runtime Trace.
- Direct and Group chat with async durable acceptance and SSE reconciliation.
- Character Space with autonomous posts, audience reactions, search/world observation, image and voice media.
- Voice Message V1 across Direct and Group.
- Vision, transient Camera/Display capture, and ImageGen.
- Settings Center, Dev Console, Media Runtime, TTS Provider Runtime/Workbench, and GSV voice-template flow.

### Engineering

- Full Linux pytest, Windows-sensitive smoke, and browser smoke are release gates.
- Started FastAPI lifecycle-warning cleanup by replacing the deprecated `@app.on_event` registration path with centralized lifecycle registration.
- Added an explicit release/version policy; stable baselines are immutable tags/releases rather than a moving stable branch.
