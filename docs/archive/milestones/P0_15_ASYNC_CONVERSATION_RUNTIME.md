# P0.15 — Asynchronous Conversation Runtime

## Problem

The old Web chat treated one HTTP request as one conversational turn:

1. user sends a message;
2. composer is disabled;
3. `/v1/chat` or group `/chat` waits for all model work;
4. only after the reaction finishes can the user send again.

That is correct for an RPC demo but wrong for an instant-messaging product. User facts and character reactions have different lifecycles.

## New contract

### User Message = durable Event

`POST /v1/chat/messages` and `POST /v1/groups/{conversation_id}/messages`:

- validate input/resources;
- persist the user Event immediately;
- enqueue a reaction request;
- return HTTP 202 without waiting for the LLM.

The composer stays enabled while characters are generating.

### Reaction = asynchronous interpretation of the event stream

`ReactionScheduler` is per conversation. It waits for a short burst window:

- quiet window: 500 ms;
- maximum burst window: 1.5 s.

Several quick user messages therefore become shared facts first and normally trigger one reaction against the newest watermark rather than one mechanical reply per sentence.

### Watermark / supersession

Every generation has a source Event ID watermark. Before any derived transaction commits, Runtime verifies that the watermark is still the latest user fact for that conversation.

If a newer user Event arrived while the model was generating, the old generation is `SUPERSEDED` and writes none of the following:

- Character message / Sticker / Image;
- Mental State;
- Memory;
- Intent;
- Runtime Trace / Action Event.

The scheduler immediately retries from the newest user fact. Superseded cycles do not advance the processed watermark or discard pending image bytes, so a follow-up can still understand an earlier image in the same unconsumed burst.

### SSE progressive delivery

`GET /v1/events/stream` delivers ephemeral UI notifications. Durable state remains SQLite and history APIs remain the reconnect/recovery source of truth.

Direct events include:

- `reaction_status` (`queued`, `typing`, `superseded`, `idle`);
- `character_event`;
- `reaction_complete`;
- `reaction_error`.

Group events additionally include:

- `group_character_event` immediately after one member commits;
- `group_member_complete` for live reply/silence summaries.

A group still reasons sequentially, preserving Character-to-Character causality, but Rin can appear immediately while Momo/Neko continue judging.

### Stable streams

The browser keeps one EventSource per active conversation. Sending another message does not tear down the stream. A fresh stream starts at the current ephemeral hub tail after history reconciliation; native EventSource reconnects use `Last-Event-ID` to fill transient gaps.

## Compatibility

The original synchronous `/v1/chat` and `/v1/groups/{id}/chat` endpoints remain for CLI/tests/older callers. They are no longer used by the Web UI.

Legacy group calls and the asynchronous scheduler resolve the same application-scoped per-group reaction lock.

## Boundaries

- The scheduler/SSE hub is in-process. SQLite remains durable, but pending reaction jobs do not survive process restart yet.
- Actual HTTP model cancellation is not required in P0.15; stale results are discarded safely at the commit gate.
- Multi-worker deployment will require a durable/shared queue and cross-process event transport before enabling more than one application worker.
