# ASR Streaming Architecture Draft

> Status: Draft architecture proposal  
> Scope: ASR capability foundation only; no production implementation in this PR  
> Related: `docs/current/ASR_P0_TECHNICAL_REPORT.md`

## 1. Purpose

The current voice stack can recognize speech, but its contract is fundamentally batch-oriented:

`microphone -> browser recording -> WAV -> POST /v1/asr -> one final string`

This makes three different problems look like one ASR problem:

1. audio capture and loss;
2. speech segmentation / endpointing;
3. speech recognition and transcript reconciliation.

The next implementation cycle should therefore **not start by replacing SenseVoice with another model**.

The target is a reliable **Streaming ASR Session** that makes these responsibilities explicit and observable.

### Target pipeline

```text
Browser Microphone
    |
    v
PCM16 / 16 kHz Audio Stream
    |
    v
ASR Session
    |
    +--> VAD --------------------+
    |                            |
    +--> Endpoint FSM <----------+
    |
    v
Streaming ASR
    |
    +--> PARTIAL transcript
    |
    +--> FINAL transcript
    |
    v
Transcript Buffer / Reconciler
    |
    +--> optional high-accuracy final pass
    |
    v
Final User Message
    |
    v
Character Runtime
```

The important boundary is:

> VAD decides whether speech is present. Endpoint policy decides whether an utterance has ended. ASR decides what was said. Transcript reconciliation decides what text is committed.

These are separate responsibilities.

---

## 2. Current-state problems

The current implementation has two recording semantics.

### Dictation

```text
getUserMedia
 -> AudioContext
 -> ScriptProcessor
 -> Float32 chunks
 -> concat
 -> downsample 16 kHz
 -> WAV
 -> POST /v1/asr
 -> Offline ASR
 -> final text
 -> input box
```

Dictation is therefore whole-recording batch recognition. The browser controls when the complete recording is submitted.

### Browser Call

The call path adds browser-side RMS endpointing:

- RMS threshold: approximately `0.025`
- speech start requires consecutive hot frames
- a short pre-roll is retained
- minimum speech duration is approximately 250 ms
- silence endpoint is approximately 900 ms by current runtime defaults
- hard segment limit is approximately 12 s
- the selected segment is converted to WAV and sent to batch ASR

This creates a second segmentation policy.

### Why this matters

A recognition error can currently originate from:

```text
capture
  -> chunk handling
  -> VAD
  -> endpoint decision
  -> audio conversion
  -> ASR model
  -> final text handling
  -> UI submission
```

Without a persistent session/segment identity, these causes cannot be distinguished reliably.

---

## 3. Architectural goals

### P0 goals

1. Dictation and Browser Call use the same ASR Session contract.
2. Audio can be delivered incrementally instead of only as a completed WAV.
3. VAD and endpointing are separate.
4. Streaming ASR can emit partial results.
5. A final result replaces the partial result for the same segment.
6. Only FINAL transcript events can create a Character Runtime user message.
7. Every utterance has a stable `segment_id`.
8. Audio boundaries, VAD boundaries, endpoint reason and ASR result are traceable.
9. The 12-second limit becomes a watchdog/forced-flush safeguard, not the normal utterance boundary.
10. Recognition completeness is preferred over minimum latency.
11. SenseVoice remains a baseline/fallback until the replacement path passes acceptance.
12. Model selection is based on the project's real speech corpus, not parameter count alone.

### Explicit non-goals

This draft does not:

- replace the current ASR provider;
- select a final production ASR model;
- add cloud ASR;
- redesign the Character Runtime;
- put partial transcripts into chat history;
- require a dedicated native mobile app;
- introduce multiple concurrent ASR providers in production before benchmarking.

---

## 4. Unified ASR Session

The core abstraction should be an explicit session.

### Lifecycle

```text
START
  |
  v
ACTIVE
  |
  +--> audio.push()
  |
  +--> partial events
  |
  +--> endpoint detected
  |
  v
FINALIZING
  |
  +--> final ASR
  |
  +--> optional final-pass ASR
  |
  v
FINAL
  |
  v
READY / CLOSED
```

A session may contain multiple speech segments.

### Suggested operations

```text
start_session()
push_audio(pcm16, timestamp)
signal_audio_end()
flush_segment(reason)
close_session()
cancel_session()
```

The exact transport is implementation detail. The semantic contract is more important than whether the first implementation uses WebSocket, SSE plus POST, or another local transport.

### Session properties

At minimum:

```json
{
  "session_id": "uuid",
  "source": "dictation | call",
  "sample_rate": 16000,
  "channels": 1,
  "encoding": "pcm_s16le"
}
```

A session must not depend on browser UI state for correctness.

---

## 5. Segment model

A session contains one or more speech segments.

Each segment gets a monotonically increasing identifier:

```json
{
  "session_id": "abc",
  "segment_id": 17
}
```

A segment is the unit of:

- VAD start/end;
- endpoint decision;
- streaming transcript;
- final transcript;
- optional final-pass correction;
- final Character Runtime submission.

### Segment states

```text
IDLE
 -> SPEECH
 -> ENDPOINT_PENDING
 -> FINALIZING
 -> FINAL
 -> SUBMITTED
```

A forced flush may transition directly from SPEECH to FINALIZING.

### Endpoint reasons

Use an explicit reason rather than inferring it later:

- `silence`
- `user_stop`
- `forced_max_duration`
- `session_end`
- `error`
- `manual_flush`

This is important for diagnosing missing words.

---

## 6. VAD is not endpointing

The new architecture must not treat a VAD threshold as an utterance boundary.

### VAD

VAD answers:

> Is speech currently present?

The preferred local direction is FSMN-VAD or an equivalent streaming VAD.

### Endpoint FSM

Endpointing answers:

> Has the speaker actually finished this utterance?

The endpoint policy may consider:

- trailing silence;
- speech duration;
- consecutive non-speech frames;
- ASR stability;
- punctuation or semantic completion where available;
- user stop;
- forced maximum duration.

The exact values should be benchmarked against the project's real Chinese speech samples.

### Important rule

A short silence such as 900 ms must not be treated as a universal truth.

Natural Chinese speech can contain pauses longer than the current threshold. Therefore the implementation should allow:

```text
VAD = silence
    -> keep segment open while endpoint policy is uncertain
    -> finalize only when policy decides the utterance ended
```

---

## 7. Streaming transcript protocol

Partial recognition must be first-class.

Suggested event:

```json
{
  "session_id": "abc",
  "segment_id": 17,
  "revision": 3,
  "kind": "partial",
  "text": "我觉得这个角色现在",
  "start_ms": 12400,
  "end_ms": 18600
}
```

Final:

```json
{
  "session_id": "abc",
  "segment_id": 17,
  "revision": 7,
  "kind": "final",
  "text": "我觉得这个角色现在已经比较稳定了",
  "start_ms": 12400,
  "end_ms": 18600
}
```

### Reconciliation rule

For a given `session_id + segment_id`:

- partial revisions update the same interim transcript;
- FINAL replaces the interim transcript;
- FINAL is idempotent;
- a FINAL event must never create a second message because a previous partial existed.

The UI may show:

```text
[正在识别]
我觉得这个角色现在已经比较稳定了
```

But chat history must only receive the final committed text.

---

## 8. Transcript Buffer / Reconciler

The transcript layer is responsible for turning ASR events into one authoritative segment result.

### Responsibilities

1. Deduplicate repeated partials.
2. Ignore stale revisions.
3. Replace partial with final.
4. Preserve segment order.
5. Prevent partial text from entering Character Runtime.
6. Apply optional final-pass replacement.
7. Emit exactly one committed user message per final segment.

### Invariant

```text
1 segment_id -> 1 committed user message
```

If a final-pass model changes the text, it updates the segment result before submission. It must not create another user message.

---

## 9. Optional final-accuracy pass

Streaming recognition and final recognition have different optimization targets.

A practical architecture is:

```text
Audio
  -> Streaming ASR
       -> partial
       -> provisional final
  -> Endpoint
       -> complete segment
  -> Optional high-accuracy ASR
       -> corrected final
  -> Reconciler
       -> commit
```

Candidate final-pass models are documented in the companion technical report:

- Fun-ASR-Nano;
- Qwen3-ASR 1.7B.

The final-pass model is **not automatically a production dependency**.

It must first prove that it reduces deletion/substitution errors enough to justify its latency and resource cost.

A slower final result is acceptable if it materially improves completeness.

---

## 10. Audio Segment Ledger

The system needs a small diagnostic record for every segment.

Suggested structure:

```json
{
  "session_id": "abc",
  "segment_id": 17,
  "audio_start_ms": 12400,
  "audio_end_ms": 18600,
  "vad_start_ms": 12520,
  "vad_end_ms": 18100,
  "endpoint_reason": "silence",
  "partial_count": 6,
  "final_revision": 7,
  "final_text": "我觉得这个角色现在已经比较稳定了",
  "asr_provider": "paraformer-streaming",
  "final_pass": "fun-asr-nano",
  "audio_duration_ms": 6200,
  "inference_ms": 840
}
```

The ledger does not need to become a large analytics system.

Its purpose is to answer:

> Did we lose the audio, cut the utterance, recognize it incorrectly, or submit the wrong transcript?

### Required diagnostic fields

At minimum:

- session id;
- segment id;
- source;
- audio duration;
- captured sample count;
- VAD start/end;
- endpoint reason;
- partial count;
- final revision;
- provider;
- final-pass provider, if any;
- final text;
- inference duration;
- error/cancel reason.

---

## 11. Capture contract

The browser should produce one canonical audio format for both Dictation and Call:

```text
PCM signed 16-bit little-endian
16 kHz
mono
```

The browser may capture at another native sample rate, but conversion should happen through one shared path.

### Required guarantees

For every pushed audio block:

- sequence number;
- timestamp or monotonic duration;
- sample count;
- byte length.

This allows the runtime to detect:

- dropped chunks;
- duplicated chunks;
- timestamp gaps;
- unexpected sample-rate changes;
- incomplete final blocks.

The implementation should not silently concatenate whatever chunks happen to arrive.

---

## 12. Browser responsibilities after migration

The browser remains responsible for:

- microphone permission;
- audio capture;
- sending PCM frames;
- rendering partial transcript;
- showing recording state;
- user stop/cancel.

The browser should **not** be the final authority for:

- utterance segmentation;
- final transcript;
- message creation;
- model selection.

This is particularly important because Dictation and Browser Call must behave consistently.

---

## 13. Server/runtime responsibilities

The Media Runtime / ASR service should own:

- ASR session lifecycle;
- audio buffering;
- VAD;
- endpoint policy;
- streaming ASR;
- partial/final event generation;
- final-pass orchestration;
- transcript reconciliation;
- segment diagnostics.

The Character Runtime should receive only committed final user messages.

This keeps voice recognition failure isolated from character conversation state.

---

## 14. Model strategy

The initial implementation should use a low-risk baseline before adding a second recognition pass.

### Stage A

```text
FSMN-VAD
  +
Paraformer-zh-streaming
```

Purpose:

- prove streaming session mechanics;
- prove no audio loss;
- prove endpoint behavior;
- expose partial/final events;
- compare against current SenseVoice.

### Stage B

```text
Paraformer streaming
  +
optional Fun-ASR-Nano final pass
```

Purpose:

- test whether final-pass recognition materially reduces omissions/substitutions.

### Stage C

```text
Paraformer streaming
  +
optional Qwen3-ASR 1.7B final pass
```

Purpose:

- test a larger local model when the target machine has sufficient VRAM/RAM.

Resource figures and caveats are maintained in `ASR_P0_TECHNICAL_REPORT.md`. Weight size, process memory, GPU VRAM allocation, and recommended GPU capacity must not be treated as interchangeable measurements.

---

## 15. Implementation roadmap

### Phase 0 — Observability

No model replacement yet.

Implement the IDs and tracing needed to see:

```capture -> VAD -> endpoint -> ASR -> transcript -> submit
```

Acceptance:

- every segment has session_id + segment_id;
- audio duration is measurable;
- endpoint reason is recorded;
- final transcript can be traced to the originating segment.

### Phase 1 — Sessionization

Replace the semantic dependency on:

```POST /v1/asr + complete WAV
```

with a session contract supporting:

```start -> push audio -> partial -> endpoint -> final -> close
```

The first transport does not need to be perfect. The contract does.

Acceptance:

- Dictation and Call use the same session abstraction;
- no partial becomes a chat message;
- one segment creates at most one final user message;
- cancellation does not leave a ghost message.

### Phase 2 — Streaming ASR

Introduce:

```FSMN-VAD + Paraformer-zh-streaming
```

Acceptance:

- continuous audio is recognized incrementally;
- partial results update one UI segment;
- final replaces partial;
- audio sequence gaps are detectable;
- 12 s is only a forced-flush watchdog;
- natural pauses do not unnecessarily split speech.

### Phase 3 — Accuracy benchmark

Benchmark at least:

- current SenseVoice baseline;
- Paraformer streaming;
- Fun-ASR-Nano;
- Qwen3-ASR 1.7B where hardware permits.

Measure:

- CER;
- deletion rate;
- substitution rate;
- insertion rate;
- endpoint loss;
- finalization latency;
- first-partial latency;
- CPU;
- RAM;
- VRAM;
- model load time;
- warm/cold behavior;
- repeated-session stability.

The project should maintain a small representative speech corpus containing:

- normal conversation;
- long sentences;
- deliberate pauses;
- fast speech;
- character names;
- project terms;
- English technical terms;
- mixed Chinese/English;
- noisy-room samples where available.

### Phase 4 — Final pass

Only if benchmark evidence supports it:

```streaming provisional final
        |
        v
high-accuracy final pass
        |
        v
reconcile
        |
        v
commit
```

Acceptance:

- no duplicate messages;
- measurable reduction in deletion/substitution errors;
- acceptable added latency;
- bounded VRAM/RAM;
- stable repeated sessions.

---

## 16. P0 acceptance gates

The ASR work is not complete when a new model can transcribe a WAV.

It is complete only when all of the following are true.

### Capture

- no unexplained audio gaps;
- sample count is consistent;
- Dictation and Call use the same capture contract.

### Segmentation

- pauses are not routinely mistaken for utterance boundaries;
- forced max duration is a safety mechanism, not the normal segmentation rule;
- every final segment has an explicit endpoint reason.

### Recognition

- partial results are visible and stable;
- final results replace partials;
- missing-word rate is measured on the project corpus;
- model comparison uses deletion/substitution/insertion metrics.

### Submission

- exactly one final user message per segment;
- partial results never enter chat history;
- retries/final-pass correction cannot duplicate messages.

### Runtime

- repeated sessions do not leak VRAM/RAM;
- no silent CPU fallback;
- ASR failure does not require restarting the whole Character Memory application;
- Media Runtime can recover after a cancelled or failed session.

### Diagnostics

Given a report such as:

> “This sentence lost the middle eight characters.”

the system must make it possible to determine whether the loss occurred during:

```capture
 -> endpoint
 -> ASR
 -> reconciliation
 -> submission
```

If we cannot identify the layer, the ASR implementation is not sufficiently observable.

---

## 17. Failure recovery

The session contract must define recovery behavior for:

- microphone permission denied;
- microphone disconnected;
- browser audio context interruption;
- runtime unavailable;
- ASR model failure;
- streaming connection loss;
- endpoint timeout;
- user cancellation;
- final-pass failure.

Recommended behavior:

```partial/stream failure
    |
    +--> preserve captured audio when possible
    |
    +--> mark segment recoverable
    |
    +--> retry/finalize once according to policy
    |
    +--> otherwise discard without creating a chat message
```

A failed voice recognition must never create an empty or corrupted Character Runtime message.

---

## 18. Historical compatibility

Existing Voice V0/V1 implementation work established useful behavior around:

- browser microphone capture;
- browser-side VAD;
- offline ASR;
- Browser Call.

Those paths should remain functional while the new session architecture is introduced.

However, their batch/offline assumptions are **historical implementation constraints**, not the target architecture.

Migration should therefore be incremental:

```existing capture
    -> compatibility adapter
    -> new ASR Session
    -> streaming pipeline
```

This avoids a large all-at-once rewrite.

---

## 19. Relationship to ASR_P0_TECHNICAL_REPORT

The two documents have different responsibilities.

### ASR_P0_TECHNICAL_REPORT.md

Answers:

- What does the current system do?
- What models are available?
- What are their known resource requirements?
- What should we benchmark?
- What evidence exists for resource consumption?

### This document

Answers:

- What should the ASR subsystem become?
- What are the runtime contracts?
- Where should segmentation happen?
- How should partial/final transcripts work?
- How do we prevent duplicate or missing messages?
- What implementation order minimizes rework?

The architecture draft should be implemented against the evidence and constraints documented in the technical report.

---

## 20. Decision summary

The immediate implementation target is:

```text
                 Unified ASR Session
                        |
                +-------+-------+
                |               |
             Dictation        Call
                |               |
                +-------+-------+
                        |
                     PCM16
                        |
                     VAD
                        |
                  Endpoint FSM
                        |
              Paraformer Streaming
                        |
                 partial/final
                        |
              Transcript Reconciler
                        |
            optional accuracy pass
                        |
                  Final Message
```

The key architectural decision is:

> **Build a reliable speech session first; choose the final ASR model second.**

This gives the project a stable foundation for accuracy improvements without repeatedly changing browser capture, endpointing, message submission, and UI behavior together.

---

## 21. Next implementation PRs

The intended implementation sequence is deliberately small:

1. **ASR Phase 0:** observability + segment ledger.
2. **ASR Phase 1:** unified ASR Session contract and compatibility adapter.
3. **ASR Phase 2:** streaming VAD/endpoint + Paraformer integration.
4. **ASR Phase 3:** corpus benchmark and resource benchmark.
5. **ASR Phase 4:** optional final-accuracy pass.

No phase should be skipped merely because a model appears to recognize a demo sentence correctly.

The acceptance target is reliable speech-to-text behavior in the actual Character Memory voice flows.
