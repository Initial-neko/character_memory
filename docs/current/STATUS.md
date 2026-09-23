# Current Status & Roadmap

> Last audited against `main`: 2026-09-23, `ac597de4`.
>
> This is the **single owner for implementation status**. Source code/tests remain the final implementation authority. Topic documents define contracts; this document answers **what is shipped on main, what is being implemented, what is accepted backlog, what is deferred, and what is deliberately not promised**.

## 1. Status vocabulary

| State | Meaning |
| --- | --- |
| **SHIPPED** | Present on current `main`, with the user/runtime path available. |
| **IN PROGRESS** | Work exists on an open PR/branch but is **not** part of current `main`. |
| **BACKLOG** | Accepted direction or known product/engineering gap, but no current implementation should be assumed. |
| **DEFERRED** | Intentionally postponed until measurements/evals/product decisions justify it. |
| **NON-GOAL** | Explicitly outside the current stable contract. It must not be described as “coming soon”. |
| **RESEARCH** | Interesting future direction without a committed delivery contract. |

Open PRs never upgrade a capability to SHIPPED. Topic docs under `docs/current/` must describe current-main behavior unless they explicitly link here and label something IN PROGRESS/BACKLOG.

## 2. Current-main capability map

| Area | State | Current main contract |
| --- | --- | --- |
| Persistent Person Runtime | SHIPPED | Persona, durable facts, Memory, Mental State, Intent, Runtime Trace and shared Person context. |
| Direct chat | SHIPPED | Durable user acceptance, async reaction scheduling, SSE reconciliation, text/sticker/image/voice/vision expression. |
| Group chat | SHIPPED | Shared room facts, ordered member reactions, mentions, archive/restore, bounded autonomous opportunities, per-member media/voice/image expression. |
| One-prompt Ensemble groups | SHIPPED | Prompt -> web research -> candidate personas -> user confirmation -> ordinary Characters + Group. |
| Character Space | SHIPPED | Feed, cursor loading, comments, likes, views, media, autonomous posting, World Observation and bounded audience reaction. |
| Random Encounter | SHIPPED | Temporary WEB/GENERATED candidates, encounter chat, accept/dismiss lifecycle and scheduler; candidates stay outside the formal character list until accepted. |
| Minimal Memory governance | SHIPPED | Inspect, pin/unpin, forget/restore and provenance-preserving correction. |
| Voice Message | SHIPPED | Direct/Group persisted voice-message lifecycle through formal TTS. |
| Browser voice call | SHIPPED | Microphone/TTS call flow exists; late-permission ownership is hardened so a retired call cannot leave an invisible live microphone. |
| Visual input | SHIPPED | Durable image attachment plus transient Camera/Display keyframes into the same PersonRuntime. |
| Visual generation | SHIPPED | Character-autonomous SELFIE/SCENE, explicit user ImageGen tool, Agnes/msimg providers. |
| Avatar | SHIPPED | Search, generated candidate batch, local ownership, from-chat/media selection. |
| Character onboarding | SHIPPED | Creation provenance, read-only persisted Persona inspector, guaranteed first avatar via ImageGen -> search -> local fallback, and best-effort existing voice-template selection; Ensemble creation converges on the same onboarding path. |
| World Activity / Pulse | SHIPPED | Shared bounded Pulse topics/comments plus independent per-character Personal Browse clocks; world observation is decoupled from Space posting and long-term Memory admission. |
| Stickers | SHIPPED | Global catalog/import, AI vision auto-tag when requested, shared Direct/Group/Space sticker use. |
| Settings Center | SHIPPED | Persistent config/secrets ownership, provider health gating, runtime-apply vs restart semantics. |
| Dev Console | SHIPPED | Health, LLM/media/visual/world/resource diagnostics and autonomy controls. LLM Usage Explorer is IN PROGRESS in #118 and is not current-main behavior yet. |
| Media/TTS Runtime | SHIPPED | SenseVoice ASR, Sherpa, Kokoro, Edge and GSV formal routing; Workbench remains separate from the stable browser-facing TTS route. |
| Mobile browser access | SHIPPED baseline | Tailscale Serve path and mobile web contract; no native Android/iOS client is claimed. |
| Life/Diary simulation | SHIPPED but frozen | Supported research code remains available; product expansion is intentionally frozen. |
| Streamlit inspector | SHIPPED but frozen | Supported diagnostic/research entrypoint; not a primary product surface. |

## 3. Work currently in progress

### PR #118 — LLM Usage Explorer v2

Status: **IN PROGRESS**

#118 is the current-main rebuild of the superseded #112 branch. Its goal is to add real model-call metering without regressing the current Dev/Settings/World Activity/Onboarding contracts.

Target contract on the PR branch:

- meter every real OpenAI-compatible `/chat/completions` HTTP attempt;
- distinguish physical HTTP attempts from one logical model call;
- record Provider/Model, Feature/Purpose, Character/Conversation/Session, latency/status/request id;
- use exact Provider-reported Token usage when available and expose Token coverage when it is not;
- attribute calls across Direct / Group / Space / Persona / Ensemble / Encounter / Life / Avatar / Visual / Sticker / Dev;
- expose Requests vs Logical Calls, Retry/Error rates, Feature/Purpose aggregation, Model aggregation and recent requests in Dev;
- correlate Runtime Trace with usage through `llm_logical_call_id`.

Until #118 merges, `DEV_CONSOLE.md` must continue to describe the current-main Dev surface and must not claim this Explorer exists.

### PR #120 — archived-character GSV reload notice

Status: **IN PROGRESS**

#120 refines the already-shipped archived-character voice lifecycle. The missing behavior is not archive/restore itself; it is truthful diagnosis of the optional GSV sidecar reload result.

Target delta:

- distinguish `reloaded`, `rejected` and `unreachable`;
- do not show a standing “sidecar is still running” warning when no sidecar exists or a timeout/gateway response makes that unknowable;
- keep an actual refusal visible and dismissible;
- preserve archive success even when optional registry refresh is unavailable.

This is a correctness/UX follow-up on an existing SHIPPED capability, not a new product surface.

## 4. Accepted product backlog

These are known product gaps. They are accepted directions, but no delivery date is implied.

### Model-call cost optimization after observability

Status: **BACKLOG after #118**

Do not redesign model orchestration from intuition before the Usage Explorer has accumulated real data.

Questions to answer first:

- how much Group cost comes from members that ultimately stay silent;
- how many HTTP attempts are Structured Output retries rather than new logical calls;
- how much Space audience/reply propagation costs per visible action;
- how much one-prompt Ensemble creation costs per created member;
- which features grow primarily because of input-context size rather than output size.

The first concrete optimization candidate is Ensemble Persona generation. Its current cost shape is approximately:

```text
1 group research call
+ N independent Persona generation calls
+ structured-output retries
```

After #118 provides evidence, evaluate bounded batch Persona generation (for example 3-4 members per batch) while preserving per-character validation/failure isolation. The same rule applies to any future Group speaker-selection optimization: measure first, then simplify the expensive path without changing Person semantics.

### Character Space / social layer

- relationship/interest-aware audience ranking;
- Link Preview fetch/render;
- push/SSE updates for Space instead of refresh/poll-only behavior;
- dedicated post-detail page;
- image/voice attachments inside comments;
- broader social/discovery semantics required to move from “society foundation” to a proved long-term society.

Existing comment Stickers and bounded threaded replies are separate from generated comment media.

### Long-term Persona / Eval

- Model-as-Judge adapter independent of Person Runtime;
- Persona Identification Accuracy;
- 10/50/100/500-turn persona-decay suites;
- 30/100/365-day continuity suites;
- Forced Recall Rate;
- customer-service-tone score;
- multi-action naturalness;
- cross-channel identity consistency across text/voice/image;
- generated visual identity continuity.

The missing piece is not another prompt rule; it is long-run evidence that the same Person remains coherent across channels and time.

### Society / relationship validation

Phase 2 remains unproven. Candidate work includes:

- relationship/interest-aware discovery;
- re-encounter quality;
- durable multi-character relationship formation;
- recommendation/discovery based on meaningful relationship signals rather than engagement;
- independent Society evals proving that autonomous interactions are not merely random activity.

## 5. Deferred product decisions

These are intentionally not implementation tickets until long-run evidence justifies them.

### Memory

- final Memory Writer algorithm;
- per-memory-type admission thresholds;
- consolidation cadence;
- forgetting / reconsolidation policy;
- final Recall ranking;
- ANN / sqlite-vec / FAISS / dedicated vector-store choice;
- relationship-specific memory layer;
- bulk rules by source/type;
- confidence/freshness models;
- automatic stale-world-fact invalidation;
- per-character “never remember this category” policies.

### Space / World cognition convergence

Direct/Group and Space/World already share PersonContextBuilder for Persona, Mental State, Memory Recall and recent facts. Higher-level planning remains channel-specific.

Do **not** force Space/World into one universal action schema until two product questions are settled:

1. what a Person may retain as long-term Memory;
2. when World Observation is ephemeral information versus a personally meaningful remembered experience.

### Scale architecture

- multiple Character Runtime workers;
- durable/shared reaction job queue;
- cross-process SSE/event transport;
- ANN-scale memory infrastructure.

SQLite + process-local schedulers are the current single-machine contract. Scale work requires measured need, not speculative infrastructure.

## 6. Active engineering debt

This section summarizes work that can become implementation once it starts causing concrete maintenance/correctness cost. Detailed rationale remains in `TECH_DEBT.md`.

| Debt | Priority | Trigger for action |
| --- | --- | --- |
| One owner for background services/lifespan | Medium | Before adding another independent autonomous scheduler/worker family. |
| Verified committed `uv.lock` | High reproducibility | Generate and verify locally; then make CI require it. |
| Deprecated `createScriptProcessor()` browser audio path | Medium | Migrate with real AudioWorklet microphone regression coverage. |
| Duplicated sidecar audio/CUDA helpers | Medium | Refactor when another sidecar change would otherwise duplicate lifecycle edits. |
| Source-string launcher tests | Medium/low | Replace as spawn/env behavior becomes harder to maintain. |
| Large edge modules / historical CSS | Observe | Split only when ownership conflicts produce repeated regressions. |
| TTS Workbench + Provider Runtime coupling | Observe | Split only if Workbench tooling starts driving production-provider lifecycle/dependency complexity. |

## 7. Current explicit non-goals

These are **not backlog promises**.

### Voice/media

- WebRTC full duplex as a stable contract;
- barge-in;
- streaming ASR partials;
- streaming TTS chunks;
- sentence-level TTS pipeline;
- generic automatic voice cloning outside the explicit VoiceDesign/template workflow;
- emotion/prosody control;
- group-call transport;
- character-initiated calls;
- persistent raw audio.

### Visual capture

- background recording;
- raw video persistence;
- raw frame-history browser;
- uploading an entire screen-sharing session;
- automatic reuse of old capture bytes across turns.

### Architecture/frameworks

No current requirement to introduce:

- LangChain / LangGraph;
- Redis / Celery;
- PostgreSQL / pgvector;
- Knowledge Graph;
- React / Next.js.

These may be reconsidered only when an actual bottleneck or product contract requires them.

## 8. Release status

Current package version on `main` is still:

```text
0.5.0rc1
```

However, `main` has moved beyond the original RC snapshot. Therefore:

- package version does **not** mean every current-main change belongs to the already-cut RC artifact;
- `CHANGELOG.md -> Unreleased` must contain post-RC merged changes;
- closed/unmerged or future PR work is not part of current `main` or the existing RC;
- a stable `0.5.0` promotion still requires a fresh docs/code audit plus real local soak after the intended merge set is frozen.

See `RELEASES.md` for branch/tag promotion rules.

## 9. Documentation maintenance rule

When code status changes:

1. **merge to main** -> update the owning topic doc if its contract changed;
2. update this file from IN PROGRESS/BACKLOG to SHIPPED where appropriate;
3. keep README at summary level and link here rather than duplicating the whole backlog;
4. keep AGENTS.md architecture/navigation rules accurate for coding agents;
5. move historical implementation detail to Git history/PRs instead of leaving stale plans in current docs.

A documentation-only PR may update this status file when concurrent feature PRs change state, but it must never describe an open PR as shipped.
