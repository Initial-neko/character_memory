# Documentation

The documentation tree is intentionally small and role-based. A reader should not need to understand old milestones or an AI tool's workflow to learn how the current project works.

## Layout

```text
docs/
├─ README.md
├─ current/       Maintained architecture and runtime contracts
├─ research/      External references and exploratory research
└─ archive/       Historical milestone notes that are still worth keeping
```

Source code and tests are the final implementation authority. `docs/current/` is the maintained explanation of that implementation.

## Start here

### Architecture and runtime

- [Architecture](current/ARCHITECTURE.md)
- [Product design](current/DESIGN.md)
- [Codebase layout](current/CODEBASE_LAYOUT.md)
- [Person runtime](current/PERSON_RUNTIME.md)
- [Conversation runtime](current/CONVERSATION_RUNTIME.md)
- [Character Space](current/CHARACTER_SPACE.md)
- [Memory](current/MEMORY.md)
- [Technical debt](current/TECH_DEBT.md)

### Media and multimodal

- [Media runtime](current/MEDIA_RUNTIME.md)
- [Voice messages](current/VOICE_MESSAGES.md)
- [TTS Workbench / Provider Runtime](current/TTS_PROVIDER_LAB.md)
- [GSV-TTS-Lite](current/GSV_TTS_EXPERIMENT.md)
- [Qwen3-TTS experiment](current/QWEN3_TTS_EXPERIMENT.md)
- [Qwen3 Voice Design tool](current/QWEN3_VOICE_DESIGN_TOOL.md)
- [Visual capture](current/VISUAL_CAPTURE.md)
- [Visual generation](current/VISUAL_GENERATION.md)
- [Avatar search](current/AVATAR_SEARCH.md)
- [Stickers](current/STICKERS.md)

### Operations and validation

- [Settings Center](current/SETTINGS_CENTER.md)
- [Dev Console](current/DEV_CONSOLE.md)
- [Mobile access](current/MOBILE_ACCESS.md)
- [Evals](current/EVALS.md)

## Research

[research/REFERENCES.md](research/REFERENCES.md) collects external systems and papers used as references. Research material is not automatically a product contract.

## Archive

`archive/` contains historical delivery notes. They are useful for understanding how the project evolved, but they may describe old ports, old providers, old non-goals, or old runtime behavior.

Historical notes never override current code or `docs/current/`.

## Documentation policy

When a feature changes durable behavior:

1. update the relevant `docs/current/` document in the same PR;
2. keep `README.md` focused on project overview, setup, architecture, and stable entry points;
3. use the PR/issue for implementation plans, checklists, review logs, and handoff notes;
4. do not add tool-specific documentation trees or generated task ledgers to `docs/`.

Git history already preserves superseded implementation plans. The working tree should describe the project people can run today.
