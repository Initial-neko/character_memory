# Project Status and Roadmap

> Snapshot: 2026-09-23  
> Current `main`: `834ae2994218d82b672fb017b667139ef2bbdbef`  
> Package version on `main`: `0.5.0rc1`

This file is the single maintained place for **what is shipped on `main`, what is being integrated, what is accepted next work, and what is intentionally deferred**.

Architecture/feature documents under `docs/current/` describe behavior that already exists on the target branch. They must not silently treat an open PR as shipped behavior.

## Status vocabulary

| State | Meaning |
| --- | --- |
| **ON MAIN** | Implemented on current `main` and expected to be covered by repository tests/contracts. |
| **INTEGRATING** | Implemented or being hardened in an open PR; do not document it as current architecture yet. |
| **NEXT** | Accepted follow-up work, but not a current runtime contract. |
| **DEFERRED** | Intentionally postponed until measurements or product evidence justify it. |
| **EXPERIMENTAL** | Optional research/tooling, not part of the formal product/runtime contract. |

## Current product/runtime baseline — ON MAIN

### Persistent Person core

- Persona, durable Events, Memory, Mental State, Intent and Runtime Trace are the persistent-person core.
- Direct, Group and Character Space are channels around the same Person; voice, vision and ImageGen do not create parallel personalities.
- Direct/Group reactions share the common reaction/context/action-materialization path while retaining channel-specific persistence and policy.

### Direct and Group conversation

- User facts are durably accepted before asynchronous model reaction.
- SSE reconciles later character output.
- Group facts are stored once in `conversation_events`; members evaluate the same shared room history.
- Group autonomy is bounded and may remain silent.
- Group member capacity is currently up to 12.

### One-prompt AI group creation

The old “create an empty real group first” lifecycle has been replaced.

Current flow:

```text
one-line prompt
  -> invisible Ensemble build
  -> public-web research + Persona drafts
  -> one confirmation
  -> create/reuse Characters
  -> create the real GroupConversation
  -> enter the group
```

Important current invariants:

- research failure does not leave a visible empty group;
- FAILED builds cannot confirm stale drafts;
- old zero-member build artifacts are hidden from the normal group list;
- one rejected/unresolvable browser candidate no longer aborts the whole research batch.

### Character creation / onboarding baseline

Current `main` already contains the Character onboarding service, creation provenance support, read-only Persona inspection surface, avatar/voice initialization hooks, and Ensemble provenance integration.

The implementation is still under active hardening; see **INTEGRATING** below before treating this area as frozen.

### Character Space / World

- durable posts, comments, reactions, views and ordered media attachments;
- autonomous opportunities, bounded audience reactions and bounded reply propagation;
- image search, ImageGen and voice media;
- public-web World Observation with rendered-page extraction and explicit trust boundaries;
- active feed prefetch and in-app media viewing;
- archived characters retain history but leave active autonomous participation.

### Voice / visual presence

- Voice Message V1 works in Direct and Group.
- Live call Session, microphone and Camera/Screen capture are separate controls.
- AI TTS output is independent from the local microphone.
- typed chat can carry recent transient visual keyframes to the pinned call target.
- Camera/Screen raw frame bytes are transient and are not persisted as normal chat attachments.
- capture ownership uses “latest request owns the stream” semantics so retired permission requests cannot silently become active later.

### Media / TTS

- Character Runtime `:8000`;
- Media Runtime `:8001` for SenseVoice/Sherpa and the formal browser `/v1/tts` route;
- Dev Console `:8002`;
- Settings Center `:8003`;
- TTS Provider Runtime + Workbench `:9002`;
- GSV sidecar `:9014` when configured.

Formal realtime TTS providers are Sherpa / Kokoro / Edge / GSV. Qwen3 remains experimental/VoiceDesign tooling rather than a formal chat provider.

### Settings / Dev ownership

- Settings Center owns persisted configuration and Secrets.
- Dev Console owns diagnostics and runtime-only tuning, not a second copy of persistent settings.
- Space / Group runtime tuning can be temporarily hot-applied from Dev without rewriting `config.yaml`.
- Random Encounter persistent settings live in Settings; Dev only exposes diagnostics/manual triggers.
- archived characters leave active voice/GSV/proactive paths while retaining `voice.yaml` and historical media.
- Dev Console uses common / advanced / diagnostic levels so the first screen stays bounded.

### Validation baseline

Repository merge gates currently distinguish:

- full pytest;
- Windows-sensitive smoke;
- browser smoke;
- separate local/live GPU/provider acceptance where CI cannot prove real model availability.

## Active integration tracks — INTEGRATING

### PR #112 — Dev LLM Usage / Token Explorer

Goal: make model usage observable by product feature rather than only through logs.

Planned/implemented on the PR branch:

- every real OpenAI-compatible request recorded separately;
- Logical Call vs HTTP Attempt distinction;
- exact provider-reported input/output/total Token usage;
- retry/error/latency statistics;
- Feature/Purpose attribution such as `GROUP/GROUP_REACTION`, `SPACE/SPACE_REPLY`, `ENSEMBLE/ENSEMBLE_PERSONA`;
- Dev summary cards, aggregation tables and recent-call table;
- Runtime Trace -> `llm_logical_call_id` correlation.

**Current-main rule:** until this PR is reconciled with current `main` and merged, `DEV_CONSOLE.md` must not claim the Usage Explorer exists.

### PR #116 — Character Onboarding v3 hardening

Current `main` already has the onboarding baseline. #116 is a clean-main hardening/rebuild track around that behavior:

- persist Persona + creation provenance;
- keep base Persona inspectable but read-only;
- guarantee a first avatar with ImageGen -> web search -> dependency-free local fallback;
- select an existing voice template when available without blocking creation when none exists;
- make Ensemble-created characters use the same onboarding path;
- avoid holding the character write lock across provider/network work;
- preserve archived-voice lifecycle behavior already on `main`.

Treat this as **hardening of an existing capability**, not a wholly missing feature.

### PR #114 — microphone acquisition follow-up

This PR remains open, but current `main` already contains the owner-ticket primitives (`micOwnerSeq / claimMicTicket`) and the maintained Visual Capture document already describes that lifecycle.

Before merge, this PR must be reconciled against current `main` to determine whether any behavioral/test delta remains. It is **not** a separate missing product capability.

## Accepted next work — NEXT

### Measure model usage before optimizing model topology

Once the LLM Usage Explorer is on `main`, use real data before changing orchestration.

Questions to answer:

- Which feature consumes the most input Token?
- How much Group cost comes from members that choose silence?
- How often Structured Output repair retries occur by feature/model?
- How much Space audience/reply propagation costs per visible action?
- How much Ensemble creation costs per created character?

Do not optimize from intuition when the runtime can provide direct measurements.

### Reduce AI-group Persona N+1 calls

Current Ensemble research is roughly:

```text
1 group research call
+ N independent Persona generation calls
+ retries when structured output fails
```

After Usage data exists, evaluate bounded batch Persona generation (for example 3-4 members per batch) while preserving per-character validation and failure isolation.

### Finish the onboarding + Ensemble acceptance loop

After onboarding hardening lands, verify the complete user path rather than only component contracts:

```text
one prompt
  -> researched member drafts
  -> one confirmation
  -> each new character has Persona + provenance + first avatar + selected/known voice state
  -> real group created
  -> group immediately usable
```

The target is to remove the old workflow where users had to visit several drawers after creation to finish a character manually.

### Release/soak work

`main` is already ahead of the `0.5.0rc1` baseline while the package version remains `0.5.0rc1`.

Before the next stable/RC decision:

- keep CI green;
- complete a real local soak of the normal stack;
- update package version / changelog / release notes together;
- do not call current `main` “stable” merely because the RC exists.

## Architecture/engineering debt — DEFERRED

These are real tracked issues but should not displace correctness work without evidence.

### Background-service lifecycle ownership

Reaction, proactive wake, Space, Group autonomy and async media workers still have process-local lifecycle ownership spread across several modules.

Introduce one typed background-service/lifespan owner before adding many more schedulers. This does **not** imply Redis/Celery.

### Dependency lock

The repository still does not publish a verified `uv.lock`. Add one only after generating and validating it from a real development environment; do not hand-write it.

### Advanced Memory governance

Minimal inspect/pin/forget/restore/correct exists. Still deferred:

- source/type bulk policies;
- confidence/freshness model;
- automatic stale-world-fact invalidation;
- knowledge graph semantics;
- “never remember this class” rules.

### Browser audio capture modernization

`createScriptProcessor()` remains supported but deprecated. A future AudioWorklet migration must preserve the current PCM/ASR behavior and pass real microphone/browser regression tests.

### Multi-process Character Runtime

SQLite is durable, but ReactionScheduler/SSE/event delivery are process-local. Multiple application workers require shared durable job/event transport before being considered correct.

### Historical CSS/module cleanup

Milestone-named CSS and some large edge modules are maintainability debt. Do not rename/split solely for aesthetics; do it when ownership or test conflicts justify the churn.

## Experimental / intentionally non-formal

- Qwen3-TTS realtime experiments and VoiceDesign tooling are not formal chat TTS providers.
- optional CosyVoice sidecar is not required by the normal stack.
- Life simulation / Streamlit inspector remain supported but product expansion is frozen.
- Space link-preview media is prepared as a type but native product rendering remains later work.
- raw continuous Camera/Screen recording and long-term frame persistence are intentionally out of scope.
- a distributed queue/vector database is not a default architectural requirement.

## Documentation audit findings

This status pass found several recurring documentation problems:

1. **No single owner for current vs next work.** Open PRs and future plans were being discussed outside the maintained docs, while feature docs were expected to stay current-only.
2. **`CHANGELOG.md` drift.** `Unreleased` still said “No queued release changes” even though `main` had already moved well past the 2026-09-22 RC baseline.
3. **Architecture vs implementation timing.** A feature document can accidentally describe an open-PR behavior before it is on `main`; this file now owns in-progress behavior instead.
4. **Code-navigation drift.** Newly important modules such as Ensemble, Encounter and Character onboarding were underrepresented in the navigation path even though they are now normal development entry points.
5. **Open PR != missing feature.** #114 demonstrates why status must be checked against current code: an open follow-up can overlap behavior already present on `main`.

## Maintenance rule

When work changes state:

```text
PR opened / scope accepted
  -> update PROJECT_STATUS.md if it is a meaningful active track

PR merged
  -> move behavior into the relevant current contract doc
  -> update PROJECT_STATUS.md
  -> update CHANGELOG.md when user-visible/architecture-significant

PR superseded/closed
  -> remove or rewrite the active-track entry
```

Do not duplicate implementation checklists, agent transcripts or per-commit logs here. The goal is a concise, human-maintained map of **what exists, what is moving, and what is intentionally later**.
