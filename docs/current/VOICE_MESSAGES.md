# Voice Messages

Status: **V1 end-to-end implementation is under review on the voice-message feature branch.**

This document describes the current `main` contract after the voice-message persistence work. It is intentionally narrower than voice calls: a voice message is a durable chat event with text plus a synthesized audio asset, not a live call transport.

## Current contract

`ActionType.VOICE_MESSAGE` exists as a text-carrying visible action. Its message text remains the durable chat content; audio state is stored in event metadata.

Canonical metadata fields are defined once in `character_memory.voice_message_fields`:

```text
voice_status
voice_media_id
voice_duration_ms
voice_error
```

State progression:

```text
pending
  ├─> ready   -> media id + optional duration
  └─> failed  -> error, no media id
```

Media storage accepts:

- `audio/wav`
- `audio/mpeg` (MP3)

and stores audio alongside other local media assets.

History/payload builders preserve the voice metadata so a persisted event can be loaded again without losing its voice state.

## State transitions

The current state-transition service can update a persisted direct-chat event and republish the same event id over SSE. Reusing the original id is important because the eventual browser client should update the existing bubble in place instead of creating a second message.

The service validates event provenance before writing so an id collision with the separate group-event table cannot modify an unrelated direct-chat row.

## V1 product flow

The V1 feature branch completes the durable voice-message path:

```text
VOICE_MESSAGE action
  -> persist pending event first
  -> one POST /v1/tts with the complete message text
  -> save WAV/MP3 through MediaStorage
  -> update the same event id to ready/failed
  -> direct/group SSE updates the existing bubble
  -> history reload reuses the persisted audio asset
```

The browser renders a compact IM-style voice bubble rather than native `<audio controls>`. The bubble shows a speaker glyph and duration, grows within a bounded width according to duration, supports play/pause, and exposes the original text on demand. Translation has a UI slot but is not a V1 backend dependency.

Only `VOICE_MESSAGE` enters this materialization path. Ordinary `MESSAGE` remains text and does not gain a durable audio asset.

## Relationship to voice calls

Voice calls and voice messages share TTS providers but have different lifecycle semantics.

```text
Voice call
  live microphone -> ASR -> normal Person reaction -> playback queue

Voice message
  durable chat event -> synthesize once -> persist audio asset -> replay later
```

Do not make a voice message depend on live-call UI state, and do not create a second Person/Memory path for it.

## Completion gate

The feature becomes end-to-end only when the same change set provides all of the following:

1. model/runtime admission of `VOICE_MESSAGE`;
2. synthesis through the configured formal TTS path;
3. persisted audio and `pending -> ready/failed` transitions;
4. browser rendering/playback;
5. focused recovery/error behavior;
6. regression coverage across persistence and client delivery.

Acceptance still requires CI plus manual browser/audio verification before this branch is merged.
