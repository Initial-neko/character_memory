# Documentation

The maintained documentation is intentionally organized by **stable domain**, not by every individual feature. Source code and tests remain the final implementation authority; `docs/current/` explains the current contract.

- [Android / External Client API Contract](current/MOBILE_API_CONTRACT.md) — source-verified Core APIs and proposed gaps; [Android consumer repository](https://github.com/Initial-neko/character_memory_android).

## Layout

```text
docs/
├─ README.md
├─ current/       Maintained current contracts
├─ research/      External references / exploratory research
└─ archive/       Historical milestone notes
```

Historical material never overrides current code or `docs/current/`.

## Current contracts

| Document | Owner |
| --- | --- |
| [STATUS](current/STATUS.md) | What is shipped / active / backlog / deferred. |
| [DELIVERY_PLAN](current/DELIVERY_PLAN.md) | Release-convergence ledger: implemented vs real-machine acceptance. |
| [DESIGN](current/DESIGN.md) | Product goal, phases and value boundaries. |
| [ARCHITECTURE](current/ARCHITECTURE.md) | Runtime topology, process/data boundaries and composition. |
| [CODEBASE_LAYOUT](current/CODEBASE_LAYOUT.md) | Module ownership and where to start reading for a change. |
| [PERSON_RUNTIME](current/PERSON_RUNTIME.md) | Character cognition/action contract. |
| [MEMORY](current/MEMORY.md) | Durable facts, admission, recall and provenance. |
| [CONVERSATION_RUNTIME](current/CONVERSATION_RUNTIME.md) | Direct/Group delivery, SSE, durable Voice Messages and Stickers. |
| [SOCIAL_WORLD](current/SOCIAL_WORLD.md) | Character Space, World Pulse and Personal Browse. |
| [VISUAL](current/VISUAL.md) | Camera/Screen capture, ImageGen and Avatar sources. |
| [VOICE_AND_TTS](current/VOICE_AND_TTS.md) | ASR/TTS runtime, providers, Workbench, GSV and Qwen tooling. |
| [SETTINGS_CENTER](current/SETTINGS_CENTER.md) | Persistent configuration and secret/apply semantics. |
| [DEV_CONSOLE](current/DEV_CONSOLE.md) | Runtime diagnostics, smoke and developer controls. |
| [EVALS](current/EVALS.md) | Behavior/regression evaluation contract. |
| [TECH_DEBT](current/TECH_DEBT.md) | Debt that still exists on current main. |
| [MOBILE_ACCESS](current/MOBILE_ACCESS.md) | Private mobile web access through Tailscale Serve. |
| [RELEASES](current/RELEASES.md) | Branch/tag/version/release promotion policy. |

## Ownership rule

Prefer extending an existing domain document. Add a new `docs/current/*.md` only when the capability has an independent runtime boundary, independent lifecycle, or a durable reason to evolve separately.

Do not create one document per provider, sidecar, UI widget, PR, implementation plan or agent task.

Implementation plans, temporary handoffs and one-off acceptance logs belong in Issues/PRs or local untracked notes. Durable behavior changes belong in the owning current contract. `DELIVERY_PLAN.md` is the deliberate exception: it is the release-convergence **acceptance ledger** that separates implementation status from target-machine proof, not a feature implementation plan.

## Research and archive

- [research/REFERENCES.md](research/REFERENCES.md) collects external references; research is not automatically a product contract.
- [ASR P0 research](research/ASR_P0_TECHNICAL_REPORT.md) preserves the original batch-path audit, model comparisons and benchmark proposals. Its risks and migration steps describe that historical baseline, not current defects or new implementation instructions.
- [ASR streaming design notes](research/ASR_STREAMING_ARCHITECTURE_DRAFT.md) preserve design rationale and proposed refinements. For current provider selection and runtime behavior, start with [VOICE_AND_TTS](current/VOICE_AND_TTS.md); for outstanding acceptance, use [DELIVERY_PLAN](current/DELIVERY_PLAN.md).
- `archive/` preserves selected historical milestones and may contain obsolete ports/providers/non-goals.
