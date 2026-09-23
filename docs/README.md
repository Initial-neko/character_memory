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

- [Current status & roadmap](current/STATUS.md) — shipped / in progress / backlog / deferred / non-goal
- [Architecture](current/ARCHITECTURE.md)
- [Product design](current/DESIGN.md)
- [Codebase layout](current/CODEBASE_LAYOUT.md)
- [Person runtime](current/PERSON_RUNTIME.md)
- [Conversation runtime](current/CONVERSATION_RUNTIME.md)
- [Character Space](current/CHARACTER_SPACE.md)
- [World Activity / Pulse](current/WORLD_ACTIVITY.md)
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

- `STATUS.md` — **唯一的实现状态 owner**：SHIPPED / IN PROGRESS / BACKLOG / DEFERRED / NON-GOAL；
- `DESIGN.md` — 产品目标、阶段与价值边界；
- `ARCHITECTURE.md` — 进程、数据边界、composition root 与主要运行流；
- `CODEBASE_LAYOUT.md` — 文件/module ownership 与“改某能力先看哪里”；
- `PERSON_RUNTIME.md` — 人物 cognition/action contract；
- `CONVERSATION_RUNTIME.md` — Direct/Group delivery、SSE 与异步一致性；
- `CHARACTER_SPACE.md` — Space social-channel contract；
- `WORLD_ACTIVITY.md` — World Pulse、Personal Browse 与独立网络观察调度 contract；
- `MEMORY.md` — Memory admission/recall/provenance 以及尚未决定的 memory policy；
- `MEDIA_RUNTIME.md` — ASR/TTS transport/runtime；
- `SETTINGS_CENTER.md` — 配置与 Secret persistence/apply semantics；
- `DEV_CONSOLE.md` — 诊断入口与 smoke workflow；
- `RELEASES.md` — branch/tag/version/release promotion policy。

其它文档引用 owner 文档，不复制整套 topology、端口、Provider 列表或同一行为定义。README 只保留可运行摘要和链接。

状态信息尤其不能散落成互相冲突的“计划”：专题文档可以写本领域的 current gap / boundary，但优先级、是否正在实现、是否只是 deferred research，一律由 `STATUS.md` 收敛。Open PR 只能标为 IN PROGRESS，不能提前进入“当前能力”。

## Documentation policy

When a feature changes durable behavior:

1. update the relevant `docs/current/` contract in the same PR;
2. update `current/STATUS.md` when the implementation state changes;
3. keep `README.md` focused on project overview, setup, architecture, stable entry points, and a short status link;
4. use the PR/issue for implementation plans, checklists, review logs, and handoff notes;
5. do not add tool-specific documentation trees or generated task ledgers to `docs/`.

Git history already preserves superseded implementation plans. The working tree should describe the project people can run today.
