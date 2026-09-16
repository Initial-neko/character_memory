# Documentation

本目录区分 **当前事实源文档**、**研究参考** 与 **历史交付记录**，避免 P0.x 迭代说明和今天的实现混在同一层级。

## 目录规则

```text
docs/
├─ README.md
├─ current/           当前实现与产品 contract
├─ research/          研究论文、外部参考与方向性材料
└─ archive/
   └─ milestones/     历史 P0.x delivery notes
```

优先级：

```text
当前源码 HEAD
  > docs/current/
  > config.example.yaml / pyproject.toml
  > docs/research/
  > docs/archive/
```

如果 `docs/archive/` 与当前代码冲突，以当前代码和 `docs/current/` 为准。Archive 的价值是解释“为什么曾经这样设计”，不是继续充当 API 或运行说明。

## Current

- [`current/ARCHITECTURE.md`](current/ARCHITECTURE.md) — 当前五服务开发栈、存储、Provider、并发和多模态边界。
- [`current/DESIGN.md`](current/DESIGN.md) — Persistent AI Person 的产品原则。
- [`current/CODEBASE_LAYOUT.md`](current/CODEBASE_LAYOUT.md) — 源码目录和模块职责导航。
- [`current/TECH_DEBT.md`](current/TECH_DEBT.md) — V1 真实技术债、已处理项与明确延后项。
- [`current/PERSON_RUNTIME.md`](current/PERSON_RUNTIME.md) — PersonReaction、Action、Mental State、Direct/Group 自主 ImageGen。
- [`current/CONVERSATION_RUNTIME.md`](current/CONVERSATION_RUNTIME.md) — Direct/Group、异步接受、SSE、supersession、Search/Mention/Archive。
- [`current/MEMORY.md`](current/MEMORY.md) — Event/Memory/Recall/Admission baseline。
- [`current/VISUAL_CAPTURE.md`](current/VISUAL_CAPTURE.md) — Camera/Screen Share、关键帧选择、transient Vision context。
- [`current/VISUAL_GENERATION.md`](current/VISUAL_GENERATION.md) — SELFIE/SCENE、自主 ImageGen、显式生图工具与 Provider。
- [`current/STICKERS.md`](current/STICKERS.md) — 内置/全局/legacy Sticker、Web import、AI tagging 与 runtime retrieval。
- [`current/MEDIA_RUNTIME.md`](current/MEDIA_RUNTIME.md) — SenseVoice ASR、正式 TTS 路由与 Windows runtime 边界。
- [`current/DEV_CONSOLE.md`](current/DEV_CONSOLE.md) — `:8002/dev` 的统一开发测试入口。
- [`current/SETTINGS_CENTER.md`](current/SETTINGS_CENTER.md) — `:8003/settings` 的配置、`.env` Secret、迁移与 backup contract。
- [`current/TTS_PROVIDER_LAB.md`](current/TTS_PROVIDER_LAB.md) — `:9002` Provider Runtime + Lab，Kokoro/Sherpa/CosyVoice 边界。
- [`current/AVATAR_SEARCH.md`](current/AVATAR_SEARCH.md) — Avatar Search 与隐私边界。
- [`current/EVALS.md`](current/EVALS.md) — Eval 与 regression 方向。

## Current runtime inventory

当前 `character-stack` 编排：

```text
:8000 Character Runtime
:8001 Media Runtime
:8002 Dev Console
:8003 Settings Center
:9002 TTS Provider Runtime + Lab
```

可选 CosyVoice sidecar 使用 `:9012`，不属于主 stack 的强制依赖。

## Research

- [`research/REFERENCES.md`](research/REFERENCES.md) — 当前方向使用过的论文/系统参考。研究资料不会自动变成产品 contract。

## Archive

[`archive/milestones/`](archive/milestones/) 保存 P0.x 历史交付说明。

这些文件中常见的以下内容尤其容易过时：

- “当前不做 Vision / Voice / Avatar / ImageGen”之类的阶段边界；
- 同步 `/v1/chat` 作为 WebUI 主链路；
- polling/SSE 的旧行为；
- 旧 TTS baseline、旧 voice 名称、旧模型准备命令；
- `uv sync --extra ...` 的局部环境安装方式；
- 旧前端 override 链与旧 CSS/JS 名称；
- 旧模型 ID、旧 Provider、旧端口或启动方式。

保留它们是为了追溯设计演进，而不是让后来开发者猜哪个版本才是真的。

## 更新约定

新增功能如果改变长期 contract，应优先更新 `docs/current/`，而不是只新建一个 `P0_XX_*.md`。

如果确实需要 milestone 说明：

1. 当前 contract 同步更新到 `docs/current/`；
2. delivery note 直接放 `docs/archive/milestones/`；
3. README 只保留稳定入口和能力摘要，不堆叠版本流水账。
