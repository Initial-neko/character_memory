# character_memory

`character_memory` 是一个用于研究 **Persistent AI Person / 持久化 AI 人物** 的实验项目。

项目当前仍以 **Prove the Person** 为核心：验证一个人物能否在长期互动中保持可辨识的人格、拥有可追溯经历、选择性记忆、持续心理状态与自主表达，并让今天的行为能够被过去解释。

语音、视觉采集、头像、图片生成、群聊等能力都是同一个人物的感知/表达渠道，不建立第二套人格：

> 不优化“人物有多喜欢用户”，而优化“人物现在为什么会这样做”。

## 项目状态

这是一个快速迭代中的实验项目。当前包版本进入 **0.5.0rc1**，作为第一条 Persistent Person Runtime release-candidate 基线；稳定版仍以真实运行验收后的不可变 Git tag / GitHub Release 为准，而不是额外维护一个会漂移的 stable 分支。

README 只描述可运行入口和已经落地的主能力；尚未完成端到端闭环的能力会明确标记为 foundation / in progress，而不是用实施计划冒充现状。

当前 `main` 已经继续向前迭代，但 package version 仍是 `0.5.0rc1`。因此“当前 main 的能力”与“已经切出的 RC artifact”不能混为一谈。**SHIPPED / IN PROGRESS / BACKLOG / DEFERRED / NON-GOAL 的统一状态表见 [Current Status & Roadmap](docs/current/STATUS.md)。**

开发和贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，仓库级工程规则见 [AGENTS.md](AGENTS.md)。文档索引见 [docs/README.md](docs/README.md)，版本与发布策略见 [docs/current/RELEASES.md](docs/current/RELEASES.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 当前能力

- 多 Character Persona；`personas/*/persona.yaml` 是人物定义事实源。
- Character onboarding：新建人物会记录 creation provenance，初始化首头像（ImageGen → Web Search → 本地 fallback），可用时选择既有 voice template；持久化 base Persona 可查看但只读。
- SQLite Event Log、Memory、Mental State、Intent、Runtime Trace。
- 本地 BGE Embedding + Vector Recall；同一进程内所有人物共享 Embedding / LLM Provider。
- `PersonReaction.actions[0..3]`：`MESSAGE / EMOJI / STICKER / IMAGE`，以及内部工具意图 `GENERATE_IMAGE`。
- `actions=[]` 是合法沉默；辅助 Memory/Intent 字段允许安全容错，主 outward action contract 仍严格。
- Direct Chat + Group Chat；群聊共享事实只保存一次，成员按因果顺序逐个判断。已有群聊还可以获得稀疏的自主交流机会：轮转 seed 可沉默，短链消息有硬上限，新 User fact 可 supersede 过时自主结果。
- One-prompt Ensemble：一句话描述群体后先做公开资料研究，生成候选成员，由用户确认后复用正式 Character + Group 生命周期。
- 异步消息接受：用户消息先持久化并立即返回 202，人物反应通过 SSE 渐进推送。
- Message Search、Group Mentions、Unread、Intent Preview、Group Archive/Restore。
- Character Space：共享帖子/评论/点赞/已查看/媒体事实；未归档角色默认每 24H 获得一次可沉默的自主发帖机会（测试时可调成 1H/30min/10min），可自然选择文字、互联网搜图、AI 生图或一条语音动态，并可在发帖前通过 Search + Headless Chromium 做受限 World Observation；Audience 最多 10 个候选角色，经同一人物状态决定忽略/点赞/评论，作者可自主回复。
- World Activity：World Pulse 周期性读取配置的聚合/趋势页形成有界共享话题，角色可独立评论；每个角色还有独立 Personal Browse 时钟。观察世界与 Space 发帖解耦，不会因为浏览就自动发动态、发私聊或直接写长期 Memory。
- Random Encounter：WEB / GENERATED 临时候选人物有独立持久化池、短期聊天、接受/忽略与定时机会；在用户确认接受前不会污染正式 Character 列表。
- 用户图片输入 + Vision；浏览器 Camera / Display Capture 会选择关键帧作为本轮 transient Vision context，不把帧二进制长期写进聊天事实。
- ImageGen：Direct 与 Group 中 Character 都可以自主选择 `SELFIE / SCENE`；同时保留用户显式“AI 生成图片”草稿工具。
- Avatar Search / Avatar Generate / 从聊天图片设头像。
- Media Runtime：SenseVoice ASR；正式 Browser TTS 固定走 `:8001/v1/tts`，按配置路由 Kokoro / Sherpa / Edge / GSV-TTS-Lite。
- TTS Workbench + Provider Runtime：`:9002` 承载正式 Provider adapter、试听/benchmark，以及可选 VoiceDesign 工具；Sherpa 试听使用独立 provider-specific Media route，不经过正式 TTS selector。
- GSV-TTS-Lite：独立 `:9014` sidecar；音色统一保存为 `voices/<name>.yaml` 模板，`personas/<character>/voice.yaml` 只引用模板；VoiceDesign freeze 会把试听结果固化成可复用模板。
- Settings Center：`config.yaml` 管正式运行选择，`.env` 管 Secret 与 GSV runtime 资产；测试阶段可直接调整 Character Space 自主开关、Opportunity Interval（默认24H，可测1H等）、Audience 数量（0~10）和 scheduler poll。Provider/Voice/Speed 与 GSV runtime 配置支持热生效；Space/Autonomous Group 行为配置可在 Dev Console 热应用；Settings Center 持久化配置后按其 runtime-apply/restart contract 生效。
- Dev Console：统一测试 LLM、ASR/TTS、ImageGen、资源与运行状态。
- Voice Message V1 已形成 Direct/Group 完整链路：`VOICE_MESSAGE` 先持久化 pending 文本事件，再走正式 TTS 合成 WAV/MP3、更新同一事件为 ready/failed，并由浏览器语音气泡播放或展示失败原因。
- pytest、Browser Smoke、JSONL Eval regression。

## 运行架构

```text
Browser
├─ Chat UI ------------------------------------------------------┐
├─ Dev Console :8002                                            │
├─ Settings Center :8003                                        │
└─ TTS Provider Lab :9002                                       │
                                                                │
Character Runtime :8000                                         │
├─ Direct / Group HTTP + SSE                                    │
├─ Character Space shared social facts                          │
├─ ReactionScheduler / PersonRuntime                            │
├─ Persona / Memory / Mental State / Intent                     │
├─ Vision / Visual Capture context                              │
├─ ImageGen / autonomous visual                                 │
├─ Character Space / World Observation / Headless Browser       │
├─ World Pulse / independent Personal Browse                     │
├─ Ensemble creation / Random Encounter                          │
└─ SQLite + local media metadata/files                          │
                                                                │
Media Runtime :8001 <-------------------------------------------┘
├─ SenseVoice ASR
├─ Sherpa VITS local runtime
├─ /v1/providers/sherpa/tts  (Workbench-only direct Sherpa route)
└─ formal /v1/tts router
      ├─ Sherpa -> local VITS
      └─ Kokoro / Edge / GSV -> TTS Provider Runtime :9002/v1/tts

Optional CosyVoice sidecar :9012
GSV-TTS-Lite sidecar :9014 (when its isolated runtime exists)
Qwen3 VoiceDesign :9015 (manual/optional tool, never a formal chat Provider)
```

这些服务是独立进程。Voice、Vision、Camera/Screen Share 与 ImageGen 都复用同一个 Persistent Person，不存在第二套“语音人物”或“视觉人物”。

## 开发环境

要求：

- Python `>=3.12,<3.13`
- 推荐 `uv`
- Windows 开发主路径支持 Git Bash

首次准备完整开发环境：

```bash
git clone https://github.com/Initial-neko/character_memory.git
cd character_memory
bash scripts/setup-media-models.sh
uv run character-memory init
```

`setup-media-models.sh` 会复用 `scripts/sync-all.sh`，同时准备本地 BGE Embedding、Sherpa ASR/TTS 与 Kokoro 模型/voice 文件。Character Runtime 的 sentence-transformers 路径是 strict-offline，正常启动/首轮聊天不会临时访问 Hugging Face。只需要重新同步 Python 开发依赖时可运行：

```bash
bash scripts/sync-all.sh
```

配置约定：

```text
config.yaml   正式运行选择与非敏感应用配置
.env          API Key / Token + GSV 本地 runtime 资产路径
```

Secret 优先通过 Settings Center 管理，也可以在系统环境变量中覆盖，例如：

```bash
export OPENCODE_GO_API_KEY="..."
export SEARCHAPI_API_KEY="..."
export AGNES_API_KEY="..."
export MSIMG_API_KEY="..."       # 或 MODELSCOPE_API_TOKEN
```

不要把真实密钥提交到仓库。

### 推荐启动方式

```bash
uv run character-stack
```

默认启动并检查：

- Character Runtime: `http://127.0.0.1:8000`
- Media Runtime: `http://127.0.0.1:8001`
- Dev Console: `http://127.0.0.1:8002/dev`
- Settings Center: `http://127.0.0.1:8003/settings`
- TTS Provider Runtime + Lab: `http://127.0.0.1:9002/tts`

可选 CosyVoice sidecar 独立运行在 `:9012`，未运行时不应阻止主 stack 启动。

`character-stack` 会复用已经健康运行的服务。修改 Runtime 代码后如果行为仍像旧版本，请先结束旧进程再重新启动，避免复用旧服务。

可选：

```bash
uv run character-stack --open chat
uv run character-stack --open settings
uv run character-stack --open tts
uv run character-stack --no-browser
```

## 常用测试

```bash
uv run pytest -q
bash scripts/sync-all.sh
uv run python scripts/benchmark_media.py --wav path/to/test.wav --iterations 20
uv run character-memory eval evals/p0_relationship.jsonl
```

Playwright Python runtime 已进入 canonical `all` extra，因为 World Observation 正式使用它；Chromium 浏览器二进制仍由独立安装步骤（CI browser job 或 `uv run playwright install chromium`）准备。

## 仓库目录

```text
.
├─ src/character_memory/    Python 主代码
├─ personas/                人物定义
├─ docs/                    当前文档、研究资料与历史里程碑
│  ├─ current/              当前事实源文档
│  ├─ research/             研究参考
│  └─ archive/              历史交付说明，不作为当前实现依据
├─ tests/                   单元/集成/浏览器 contract tests
├─ evals/                   JSONL Eval 数据
├─ scripts/                 开发、Media、同步脚本
├─ config.example.yaml      配置示例
└─ pyproject.toml           Python 依赖与入口
```

代码模块导航见 [`docs/current/CODEBASE_LAYOUT.md`](docs/current/CODEBASE_LAYOUT.md)。

## 文档入口

**当前实现以源码为最终事实源。** 当前文档集中在 [`docs/current/`](docs/current/)：

- [Current Status & Roadmap](docs/current/STATUS.md)
- [Architecture](docs/current/ARCHITECTURE.md)
- [Product Design](docs/current/DESIGN.md)
- [Codebase Layout](docs/current/CODEBASE_LAYOUT.md)
- [Person Runtime](docs/current/PERSON_RUNTIME.md)
- [Conversation Runtime](docs/current/CONVERSATION_RUNTIME.md)
- [Character Space](docs/current/CHARACTER_SPACE.md)
- [World Activity](docs/current/WORLD_ACTIVITY.md)
- [Memory](docs/current/MEMORY.md)
- [Visual Capture](docs/current/VISUAL_CAPTURE.md)
- [Visual Generation](docs/current/VISUAL_GENERATION.md)
- [Stickers](docs/current/STICKERS.md)
- [Media Runtime](docs/current/MEDIA_RUNTIME.md)
- [Voice Messages](docs/current/VOICE_MESSAGES.md)
- [Dev Console](docs/current/DEV_CONSOLE.md)
- [Settings Center](docs/current/SETTINGS_CENTER.md)
- [TTS Provider Lab](docs/current/TTS_PROVIDER_LAB.md)
- [Avatar Search](docs/current/AVATAR_SEARCH.md)
- [Evals](docs/current/EVALS.md)

`docs/archive/milestones/` 保存 P0.x 历史交付说明，用于理解演进过程，但其中“当前不做”“当前入口”“轮询频率”等描述可能已经被后续版本取代，不能覆盖源码和 `docs/current/`。

## 核心工程原则

1. **Durable Facts 是事实源。** Direct Event、Group shared event、Space shared facts 等真实发生的记录优先；Memory、Mental State、Diary、Trace 都是派生认知层。
2. **人物可以沉默。** 用户输入不意味着必须回复。
3. **辅助认知失败不应轻易吞掉有效主回复。** 但 outward action 本身仍需要明确合法。
4. **群聊事实只保存一次。** 不把同一房间消息复制成多个彼此独立的“事实”。
5. **多模态仍是同一个人。** Voice/Vision/Capture/ImageGen 都不得绕过 PersonRuntime 另建人格状态。
6. **慢能力隔离。** ASR/TTS、ImageGen 等不能因为失败而破坏已经成立的文本主链路。
7. **先测量再复杂化。** 不因为“以后可能需要”提前引入 Redis、Celery、向量数据库、LangGraph 或大型前端框架。
8. **历史文档不是当前 contract。** 当前 HEAD 与 `docs/current/` 优先。

## License

This project is licensed under the [MIT License](LICENSE).
