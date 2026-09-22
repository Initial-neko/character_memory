# Voice Messages

Status: **V1 is implemented on current `main` for Direct and Group chat.**

Voice Message is a durable chat expression, not a live call transport. The text remains the canonical message body; synthesized audio is an attached MediaAsset that can fail without deleting the message.

## Current flow

```text
PersonReaction VOICE_MESSAGE
  -> persist CHARACTER_MESSAGE with text + voice_status=pending
  -> publish pending event
  -> VoiceMessageMaterializer
  -> POST Media Runtime :8001/v1/tts with the complete message text
  -> save WAV/MP3 through MediaStorage
  -> update the same event id
       ready  -> voice_media_id + optional duration
       failed -> voice_error, text remains readable
  -> republish the same Direct/Group event id over SSE
  -> browser merges the update in place
  -> compact voice bubble playback + always-visible text underneath
```

Canonical metadata is defined in `character_memory.voice_message_fields`:

```text
voice_status
voice_media_id
voice_duration_ms
voice_error
```

The state transition is:

```text
pending
  ├─ ready
  └─ failed
```

A failed TTS provider never removes the message text. The failure reason carried by the provider chain is preserved in `voice_error` and surfaced by the browser bubble.

## Direct and Group

Direct events live in `events`; Group events live in `conversation_events`. Both use the same `VOICE_MESSAGE` action contract and formal TTS path.

Direct `TIME_TICK` wakes and due `PROACTIVE_INTENT` turns may also choose `VOICE_MESSAGE`. They persist the same pending character event and reuse the same direct publication/materialization hook, so background speech is synthesized instead of remaining a text-only or permanently-pending voice action. Autonomous Group Chat already follows the Group materializer path.

The scheduler/materializer updates the **original event id** instead of appending a second message. Browser Direct and Group history/SSE projections preserve the voice fields and merge by id.

## Relationship to voice calls

```text
Voice call
  microphone -> ASR -> normal Person reaction -> ephemeral playback pipeline

Voice message
  Person reaction -> durable text event -> synthesize once -> persisted audio -> replay later
```

They may use the same configured TTS provider, but a durable Voice Message does not depend on live-call UI state.

## Boundaries

- One `VOICE_MESSAGE` is one complete TTS request; no sentence/chunk splitting in V1.
- Ordinary `MESSAGE` remains text-only and is not automatically materialized.
- Audio is MediaAsset data, not Memory.
- Every Voice Message shows its canonical text directly below the voice bubble. There is no WeChat-style “tap to convert/show text” step and no placeholder “翻译” button.
- The always-visible line is the canonical spoken text/transcript, not a separate machine-translation backend contract.
- Space Voice Post is implemented as a separate social-channel media intent: one complete `VOICE` intent synthesizes through the same formal `:8001/v1/tts` route and persists as a Space MediaAsset. Its stored transcript is also shown directly below the voice bubble. It does not reuse chat `VOICE_MESSAGE` event semantics.
- Raw microphone audio remains a transport/input concern and is not persisted as character memory by default.

## Main modules

```text
domain/models.py
    VOICE_MESSAGE action contract

voice_message_fields.py
    canonical persisted metadata keys/defaults

application/voice_message_materializer.py
    formal TTS -> MediaAsset -> ready/failed

application/voice_message_service.py
    durable state transitions + SSE republish

application/async_conversation.py
    Direct/Group scheduling hook

web/app.js
web/groups.js
    Direct/Group voice bubble projection/playback

space_media_executor.py
web/space.js
    autonomous Space VOICE synthesis + durable media relation + native playback
```

Regression coverage lives in `test_voice_message_*.py`, including persistence, materialization, Direct/Group transition and browser contract tests.
