# Current Status & Roadmap

> Last audited against product `main`: 2026-09-23, `2f61ead2`.
>
> This file owns **implementation status only**. Source/tests remain authoritative; the other topic documents define current behavior contracts.

## Status vocabulary

| State | Meaning |
| --- | --- |
| **SHIPPED** | Present on current `main` and available through the documented runtime/user path. |
| **IN PROGRESS** | Active implementation or acceptance work exists but is not yet part of the stable contract. |
| **BACKLOG** | Accepted gap/direction without a delivery promise. |
| **DEFERRED** | Intentionally postponed until measurements, evals or product evidence justify it. |
| **NON-GOAL** | Explicitly outside the current contract. |
| **RESEARCH** | Exploratory direction without delivery commitment. |

An open PR never upgrades a capability to SHIPPED.

## Current capability map

| Area | State | Current contract |
| --- | --- | --- |
| Persistent Person Runtime | SHIPPED | Persona, durable facts, Memory, Mental State, Intent, Runtime Trace and shared Person context. |
| Direct chat | SHIPPED | Durable accept, async reaction scheduling, SSE reconciliation and multimodal expression. |
| Group chat | SHIPPED | Shared facts, ordered reactions, mentions, archive/restore and bounded autonomous opportunities. |
| One-prompt Ensemble | SHIPPED | Prompt -> research -> candidates -> confirmation -> ordinary Characters + Group. |
| Character onboarding | SHIPPED | Provenance, persisted Persona, guaranteed first avatar and best-effort voice selection. |
| Character Space | SHIPPED | Feed, comments/replies, likes/views, up to 9 media, autonomous posting and bounded audience propagation. |
| World Activity | SHIPPED | World Pulse plus independent per-character Personal Browse clocks; observation is not automatic publishing. |
| Random Encounter | SHIPPED | Temporary WEB/GENERATED candidates, trial chat, accept/dismiss lifecycle and scheduler. |
| Memory governance | SHIPPED | Inspect, pin/unpin, forget/restore and provenance-preserving correction. |
| Voice Message | SHIPPED | Direct/Group durable voice-message lifecycle through formal TTS. |
| Browser call | SHIPPED | Microphone/TTS call flow with independent visual capture and hardened late-permission ownership. |
| Visual capture | SHIPPED | Camera/Display keyframes as transient current-turn Vision context. |
| Visual generation / Avatar | SHIPPED | SELFIE/SCENE ImageGen, explicit generation, avatar search/generation/local ownership. |
| Settings Center | SHIPPED | Persistent config/secrets ownership and apply/restart semantics. |
| Dev Console | SHIPPED | Common/advanced/diagnostic layers, runtime diagnostics and LLM Usage Explorer. |
| LLM Usage telemetry | SHIPPED | Requests vs logical calls, provider/model/feature attribution, token coverage, retries/errors and latency. |
| Media/TTS Runtime | SHIPPED | SenseVoice ASR; Sherpa/Kokoro/Edge/GSV formal routing; Workbench and Qwen tooling remain bounded. |
| Mobile browser access | SHIPPED baseline | Private Tailscale Serve path; no native Android/iOS client is claimed. |

## Current validation work

### Real Chromium frontend acceptance

Status: **IN PROGRESS**

Issue #121 owns the current real-stack browser acceptance pass. It validates Chat, Group, Character creation, Space, Settings, Dev Console, microphone/visual controls and responsive layout against the served application rather than source-string assertions.

Failures should become focused PRs with browser evidence and regression coverage where practical.

## Accepted backlog

### Model-call cost optimization

Status: **BACKLOG**

LLM Usage Explorer is now shipped, so optimization should be chosen from measured runtime data rather than intuition. Primary questions include Group silence cost, structured-output retries, Space propagation cost and Ensemble Persona generation cost.

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

- WebRTC full-duplex voice, barge-in or streaming ASR/TTS;
- persistent raw camera/screen/audio recording;
- automatic reuse of old visual-capture bytes;
- native Android/iOS clients;
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
