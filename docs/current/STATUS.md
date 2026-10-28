# Current Status & Roadmap

> Character / World implementation status reviewed: 2026-10-28. Media and real-device acceptance remain subject to their existing validation gates.
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

An open PR never upgrades a capability to SHIPPED. Implementation on main does not complete the separate real-runtime or human visual acceptance gates; current evidence and remaining sign-off belong to the owning Issues.

## Current capability map

| Area | State | Current contract |
| --- | --- | --- |
| Persistent Person Runtime | SHIPPED | Persona, durable facts, Memory, Mental State, finite Intent deferral, Runtime Trace and shared Person context with bounded historical observation recall. |
| Direct chat | SHIPPED | Durable accept, async reaction scheduling, SSE reconciliation and multimodal expression. |
| Group chat | SHIPPED | Shared facts, ordered reactions, mentions, archive/restore, member add/remove, turn-level rendering and bounded autonomous opportunities. User-turn speaker cap defaults to 5 (1–12 configurable); persisted Group Events remain action-granular. |
| One-prompt Ensemble | SHIPPED | Prompt -> research -> candidates -> confirmation -> ordinary Characters + Group. Curated warm-isekai drafts preview without model/search calls; a four-field user-authored scene uses the existing original generation path and preserves input on failure. Both confirm fresh Characters + Group, not a scoped scenario runtime. |
| Character onboarding | SHIPPED | Provenance, persisted Persona, guaranteed first avatar and best-effort voice selection. |
| Character Space | SHIPPED | Feed, comments/replies, likes/views, up to 9 media, autonomous posting and bounded audience propagation. |
| World Activity | SHIPPED | World Pulse plus independent per-character Personal Browse clocks, bounded web/RSS choices and local recovery of complete saved appraisals. Observation is not automatic publishing or Memory admission. |
| Capability discovery | SHIPPED | Per-turn trusted descriptions reflect channel, role/Skill grants, provider readiness and durable budgets; descriptions never grant execution or change the typed-JSON provider protocol. |
| Continuous public Browser | SHIPPED opt-in | Opportunity-local Playwright open/observe/follow/close sessions retain actual source scope; public-network, redirect and same-origin boundaries remain enforced. |
| Bounded World continuation | SHIPPED opt-in | At most three outer decisions consume actual tool receipts before continuing or stopping; no new scheduler, automatic posting, Memory admission or unknown-result replay. |
| Role collaboration | SHIPPED opt-in | Granted contact uses existing Group/Space shared facts and recipient reasoning, with bilateral durable receipts and channel-correct delivery. External Issue writes require a separate operator grant; without it no POST occurs. |
| Prompt composition | SHIPPED | Pure Persona, State/Memory, Perception, Capabilities and Runtime Policy parts preserve channel-specific schemas, privacy and server execution ownership. |
| RSS collection / personal reading | SHIPPED opt-in | Shared source/feed inventory is separate from character-local selected Feed-text readings. Unread inventory is not personal experience. Settings defaults and scope belong to RSS_SOURCES and SOCIAL_WORLD. |
| Stateless Claude World backend | SHIPPED opt-in foundation | Optional WEB_SEARCH backend uses fresh `claude -p` tasks and a single controlled World tool. Default remains direct. CLI/tool fixture validation passed; live Persona/public-source quality and deployment activation are separate. See ARCHITECTURE. |
| Character life / execution inspection | SHIPPED | Read-only real event/state/Memory/Intent timeline and per-character external task/reply/source/tool/usage details. Continuation step/source results and bilateral collaboration facts are projected without model calls. Indexed receipt lookups keep relationship reads from scanning all ACTION rows under the store lock; complete intermediate tool transcripts are not implemented. |
| Random Encounter | SHIPPED | Temporary WEB/GENERATED candidates, trial chat, accept/dismiss lifecycle and scheduler. |
| Activity lifecycle | SHIPPED | P10 create/read/pause/resume/end with version CAS, request-id idempotency, formal-vs-activity-local participants and a frozen SIMULATED scope. P11 adds user-authored 2–3 partner relationship practice: distinct views, simulated replies, the existing draft/edit/free-input path and explicit failed rounds. P12 adds branch points with isolated branch transcripts and consequences, retryable branch generation, ended-only review with durable retry, and one explicit adoption path that writes exactly one REAL `PRACTICE_ADOPTED` Event plus one REAL `SHARED` Memory. Unadopted practice facts stay in scenario tables; reads never load the model. No cross-scenario continuity or P13 DSH participation. See [ACTIVITY](ACTIVITY.md). |
| DSH external authorization | BACKLOG — contract frozen | P13 subject identity, explicit grant scopes, expiry/quota, immediate revocation, typed Activity actions, read boundaries and hash-chained audit are frozen but have no implementation, routes, migration or UI. See [DSH_AUTHORIZATION](DSH_AUTHORIZATION.md). |
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

Existing deterministic tests cover historical observations beyond the recent
window, character/source isolation, future-time barriers, finite Intent deferral
and bounded model-call paths. These do not prove seven-day relationship quality
or natural Persona evolution. Human acceptance of unread-boundary/expression
samples must not be generalized to post-reading quality or long-term continuity.

### Explicit external-assistance decisions and richer execution records

Status: **SHIPPED opt-in foundation / BACKLOG richer records**

Granted Direct tasks and bounded role-contact research reuse the existing Person
context, isolated Claude runner, permission checks, budgets and receipt ledger.
Current capability descriptions control what can be requested; no private Direct
or Group history is automatically forwarded to an external assistant. The actual
result is interpreted separately from the Person's own observations. Native tool
calling remains a compatibility research/probe path; production uses typed-JSON.
Complete tool transcripts and a dedicated Memory-page execution link remain
backlog, not evidence of another agent runtime or unbounded autonomous loop.

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

`v0.5.0` is the current stable baseline — an immutable tag on `main` — and the rollback point for the character-core architecture work that follows. The package version reports `0.5.0`, replacing the `0.5.0rc1` candidate that `main` outgrew over 218 commits of integration work. Version/tag/release-branch work stays separate from ordinary daytime integration.

See [RELEASES.md](RELEASES.md) for the release contract.

## Maintenance rule

When status changes:

1. behavior contract changes -> update the owning topic document;
2. implementation state changes -> update this file;
3. README remains summary-level;
4. implementation plans, acceptance notes and agent handoffs stay in Issues/PRs;
5. do not create a new current document merely because a feature or provider exists—extend the owning domain document unless it has an independent long-term boundary.
