# Current Status & Roadmap

> Last audited against product `main`: 2026-10-05, `8f7dd9381267242e95a4b3c1c4c40b1ce7dc557d`.
>
> This file owns **implementation status only**. Source/tests remain authoritative; the other topic documents define current behavior contracts.

## Status vocabulary

| State | Meaning |
| --- | --- |
| **SHIPPED** | Present on current `main` and available through the documented runtime/user path. |
| **IN PROGRESS** | Active implementation or acceptance work exists but is not yet part of the stable contract. |
| **BACKLOG** | Accepted gap/direction without a delivery promise. |
| **DEFERRED** | Intentionally postponed until measurements, evals or product evidence justify them. |
| **NON-GOAL** | Explicitly outside the current contract. |
| **RESEARCH** | Exploratory direction without delivery commitment. |

An open PR never upgrades a capability to SHIPPED.

## Current capability map

| Area | State | Current contract |
| --- | --- | --- |
| Persistent Person Runtime | SHIPPED | Persona, durable facts, Memory, Mental State, Intent, Runtime Trace and shared Person context. |
| Direct chat | SHIPPED | Durable accept, async reaction scheduling, SSE reconciliation and multimodal expression. |
| Group chat | SHIPPED | Shared facts, ordered reactions, mentions, archive/restore, member add/remove, turn-level rendering and bounded autonomous opportunities. User-turn speaker cap defaults to 5 (1–12 configurable); persisted Group Events remain action-granular. |
| One-prompt Ensemble | SHIPPED | Prompt -> research -> candidates -> confirmation -> ordinary Characters + Group. |
| Character onboarding | SHIPPED | Provenance, persisted Persona, guaranteed first avatar and best-effort voice selection. |
| Character Space | SHIPPED | Feed, comments/replies, likes/views, up to 9 media, autonomous posting and bounded audience propagation. |
| World Activity | SHIPPED | World Pulse plus independent per-character Personal Browse clocks; observation is not automatic publishing. |
| Random Encounter | SHIPPED | Temporary WEB/GENERATED candidates, trial chat, accept/dismiss lifecycle and scheduler. |
| Memory governance | SHIPPED | Inspect, pin/unpin, forget/restore and provenance-preserving correction. |
| Voice Message | SHIPPED | Direct/Group durable voice-message lifecycle through formal TTS. |
| Browser call | SHIPPED | Shared streaming ASR is preferred when Media Runtime advertises it, with batch fallback; speak-over-reply, TTS, pending turns, visual context and hardened late-permission ownership are retained. |
| Visual capture | SHIPPED | Camera/Display keyframes remain transient; DISPLAY frames preserve up to ~1440px long side, and direct screen sharing can create bounded periodic `VISUAL_OBSERVATION` opportunities on significant changes. |
| Visual generation / Avatar | SHIPPED | SELFIE/SCENE ImageGen, explicit generation, avatar search/generation/local ownership. |
| Settings Center | SHIPPED | Persistent config/secrets ownership and apply/restart semantics. |
| Dev Console | SHIPPED | Common/advanced/diagnostic layers, runtime diagnostics and LLM Usage Explorer. |
| LLM Usage telemetry | SHIPPED | Requests vs logical calls, provider/model/feature attribution, token coverage, retries/errors and latency. |
| Media/TTS Runtime | SHIPPED | SenseVoice batch ASR plus Paraformer streaming-session support; Dictation/Call prefer streaming when health advertises it and retain batch fallback. Sherpa/Kokoro/Edge/GSV formal TTS routing remains supported. |
| Mobile browser access | SHIPPED baseline | Private Tailscale Serve path remains the supported browser deployment path. |
| Native Android client | IN PROGRESS | The separate `character_memory_android` repository contains the V1 chat/voice/sticker/visual client source and synthetic CI coverage. The project user reports real-device multimedia acceptance complete (Core CI cannot reproduce it). Canonical cross-client Direct identity, Web history migration and synchronized read cursors are explicitly out of scope; device-scoped API permissions are a separate optional security consideration. Release hardening and auditable device evidence remain separate. Native iOS is not implemented. |

## Current validation work

### Real Chromium frontend acceptance

Status: **IN PROGRESS**

Issue #121 owns the current real-stack browser acceptance pass. It validates Chat, Group, Character creation, Space, Settings, Dev Console, microphone/visual controls and responsive layout against the served application rather than source-string assertions.

Failures should become focused PRs with browser evidence and regression coverage where practical.

### P0 convergence / acceptance

The current implementation baseline has converged enough that release work should now distinguish code completion from real-machine proof. The authoritative execution checklist is [DELIVERY_PLAN.md](DELIVERY_PLAN.md). New work should primarily close acceptance gaps or measured regressions rather than add unrelated surface area.

The following P0 acceptance items remain:

1. **Real Chromium end-to-end pass** — Chat, Group, Character creation, Space, Settings, Dev Console, microphone/visual controls and responsive layout.
2. **Group Chat closure** — verify turn-level folding, media-in-turn rendering, speaker cap, @-mention behavior, member add/remove and large-group readability in real Chromium.
3. **Voice/TTS closure** — verify provider/voice selection, Qwen3 VoiceDesign enablement path, Direct/Group voice messages and Call/TTS against the real media runtime.
4. **Settings/runtime consistency** — verify persisted vs runtime-applied state and restart-required behavior for Provider, Voice, Device and group speaker cap.
5. **Character onboarding closure** — verify that a newly created Character reaches usable Persona + Avatar + Voice-or-explicit-setup + first chat without a manual recovery path.
6. **Failure recovery** — verify local recovery for microphone/camera/display permission denial, TTS/media-runtime failure, SSE disconnect and Character-generation failure without forcing a full application restart.

Only correctness fixes required by these checks should create the next implementation work.

## Accepted backlog

### Model-call cost optimization

Status: **IN PROGRESS**

LLM Usage Explorer is shipped and two mechanical-call reductions are already on main: Personal Browse can skip repeated planning after a durable `browse=false` result when no new person signal exists, and Space no longer fills every configured cold Audience slot with a paid reaction (at most two deterministic cold exploration slots; existing public social ties are prioritized). The next step is runtime soak measurement, especially `SPACE_REPLY`, Group silence cost, structured-output retries and periodic Vision silence rate.

### Long-run Person / Society validation

Status: **BACKLOG / RESEARCH**

Still missing are long-horizon persona-decay and continuity suites, stronger cross-channel identity evaluation, and evidence that autonomous multi-character interaction produces durable relationships rather than random activity.

### Social layer expansion

Status: **BACKLOG**

Possible later work includes relationship-aware discovery, link previews, push/SSE Space updates, richer post detail, and generated media in comment threads. None is a current delivery promise.

## Deferred decisions

Keep these deferred until evidence requires them:

- final Memory Writer / consolidation / forgetting policy;
- final Recall ranking and ANN/vector-store choice;
- relationship-specific memory layer;
- universal action schema across Direct/Group/Space/World;
- multi-worker Character Runtime, distributed job queues or Redis/Celery;
- framework migrations such as React/Next.js or LangGraph.

## Explicit non-goals

Current stable contract does not promise:

- WebRTC media transport or streaming TTS; browser audio input currently uses the shipped WebSocket streaming-ASR session when available;
- persistent raw camera/screen/audio recording;
- automatic reuse of old visual-capture bytes;
- a native iOS client; Android is an active integration/acceptance track rather than a stable Core release contract;
- distributed runtime infrastructure without measured need.

## Release state

The package version remains `0.5.0rc1`, while `main` has moved beyond the original RC snapshot. Version/tag/release-branch work is intentionally separate from normal daytime integration and requires a fresh freeze + real local soak before stable promotion.

See [RELEASES.md](RELEASES.md) for the release contract.

## Maintenance rule

When status changes:

1. behavior contract changes -> update the owning topic document;
2. implementation state changes -> update this file;
3. README remains summary-level;
4. implementation plans, acceptance notes and agent handoffs stay in Issues/PRs;
5. do not create a new current document merely because a feature or provider exists—extend the owning domain document unless it has an independent long-term boundary.
