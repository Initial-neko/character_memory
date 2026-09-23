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

- [Project status / roadmap](current/PROJECT_STATUS.md)
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

- [Release and version policy](current/RELEASES.md)
- [Settings Center](current/SETTINGS_CENTER.md)
- [Dev Console](current/DEV_CONSOLE.md)
- [Mobile access](current/MOBILE_ACCESS.md)
- [Evals](current/EVALS.md)

## Research

[research/REFERENCES.md](research/REFERENCES.md) collects external systems and papers used as references. Research material is not automatically a product contract.

## Archive

`archive/` contains historical delivery notes. They are useful for understanding how the project evolved, but they may describe old ports, old providers, old non-goals, or old runtime behavior.

Historical notes never override current code or `docs/current/`.

## Ownership rule

`docs/current/` 可以有多份专题文档，但同一个事实只应有一个主要 owner：

- `PROJECT_STATUS.md` — 当前 main / open integration / accepted next work / deferred work 的唯一状态 owner；
- `DESIGN.md` — 产品目标、阶段与价值边界；
- `ARCHITECTURE.md` — 进程、数据边界、composition root 与主要运行流；
- `CODEBASE_LAYOUT.md` — 文件/module ownership 与“改某能力先看哪里”；
- `PERSON_RUNTIME.md` — 人物 cognition/action contract；
- `CONVERSATION_RUNTIME.md` — Direct/Group delivery、SSE 与异步一致性；
- `CHARACTER_SPACE.md` — Space / World social-channel contract；
- `MEMORY.md` — Memory admission/recall/provenance 以及尚未决定的 memory policy；
- `MEDIA_RUNTIME.md` — ASR/TTS transport/runtime；
- `SETTINGS_CENTER.md` — 配置与 Secret persistence/apply semantics；
- `DEV_CONSOLE.md` — 诊断入口与 smoke workflow；
- `RELEASES.md` — branch/tag/version/release promotion policy。

其它文档引用 owner 文档，不复制整套 topology、端口、Provider 列表或同一行为定义。README 只保留可运行摘要和链接。

## Documentation policy

When a feature changes durable behavior:

1. current behavior belongs in the relevant `docs/current/` contract only after it exists on the target branch;
2. meaningful open/integration tracks belong in `PROJECT_STATUS.md`, not prematurely in current architecture docs;
3. update `CHANGELOG.md` for user-visible or architecture-significant changes after they land;
4. keep `README.md` focused on project overview, setup, architecture, stable entry points, and links;
5. use the PR/issue for implementation plans, checklists, review logs, and handoff notes;
6. do not add tool-specific documentation trees or generated task ledgers to `docs/`.

Git history already preserves superseded implementation plans. The working tree should describe the project people can run today.
