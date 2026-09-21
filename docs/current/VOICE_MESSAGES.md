# Voice Messages

Status: **foundation implemented; end-to-end user flow is not complete yet**.

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

## What is deliberately not complete

The current tree does **not** yet provide the full product flow:

- the model is not yet allowed to choose `VOICE_MESSAGE` in its normal action whitelist;
- no synthesis worker currently takes a pending voice message through the formal TTS route and stores the result automatically;
- the browser does not yet render the voice-message player/bubble contract;
- group-chat voice-message state transitions are not implemented;
- stale `pending` messages are not recovered after a process crash;
- `/v1/media/{id}` is still the general media download route and has not been redesigned into a seekable audio streaming endpoint.

These are follow-up tasks, not hidden behavior.

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

Until then, documentation and UI should describe voice messages as an in-progress capability rather than a finished chat feature.
