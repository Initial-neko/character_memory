# character_memory

`character_memory` 是一个用于研究 **Persistent AI Person / 持久化 AI 人物** 的实验项目。

项目当前仍以 **Prove the Person** 为核心：验证一个人物能否在长期互动中保持可辨识的人格、拥有可追溯经历、选择性记忆、持续心理状态与自主表达，并让今天的行为能够被过去解释。

语音、视觉采集、头像、图片生成、群聊等能力都是同一个人物的感知/表达渠道，不建立第二套人格：

> 不优化“人物有多喜欢用户”，而优化“人物现在为什么会这样做”。

## 当前能力

- 多 Character Persona；`personas/*/persona.yaml` 是人物定义事实源。
- SQLite Event Log、Memory、Mental State、Intent、Runtime Trace。
- 本地 BGE Embedding + Vector Recall；同一进程内所有人物共享 Embedding / LLM Provider。
- `PersonReaction.actions[0..3]`：`MESSAGE / EMOJI / STICKER / IMAGE`，以及内部工具意图 `GENERATE_IMAGE`。
- `actions=[]` 是合法沉默；辅助 Memory/Intent 字段允许安全容错，主 outward action contract 仍严格。
- Direct Chat + Group Chat；群聊共享事实只保存一次，成员按因果顺序逐个判断。
- 异步消息接受：用户消息先持久化并立即返回 202，人物反应通过 SSE 渐进推送。
- Message Search、Group Mentions、Unread、Intent Preview、Group Archive/Restore。
- 用户图片输入 + Vision；浏览器 Camera / Display Capture 会选择关键帧作为本轮 transient Vision context，不把帧二进制长期写进聊天事实。
- ImageGen：Direct 与 Group 中 Character 都可以自主选择 `SELFIE / SCENE`；同时保留用户显式“AI 生成图片”草稿工具。
- Avatar Search / Avatar Generate / 从聊天图片设头像。
- Media Runtime：SenseVoice ASR；正式 Browser TTS 固定走 `:8001/v1/tts`，按配置使用 Kokoro 或 Sherpa。
- TTS Provider Runtime + Lab：`:9002` 承载 Kokoro/Sherpa/CosyVoice provider audition，其中 Kokoro 也是 V1 正式默认 TTS provider。
- Settings Center：`config.yaml` 管非敏感配置，`.env` 管 Secret；修改后统一重启 stack 生效。
- Dev Console：统一测试 LLM、ASR/TTS、ImageGen、资源与运行状态。
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
├─ ReactionScheduler / PersonRuntime                            │
├─ Persona / Memory / Mental State / Intent                     │
├─ Vision / Visual Capture context                              │
├─ ImageGen / autonomous visual                                 │
└─ SQLite + local media metadata/files                          │
                                                                │
Media Runtime :8001 <-------------------------------------------┘
├─ SenseVoice ASR
├─ Sherpa VITS fallback
└─ formal /v1/tts router
      └─ Kokoro -> TTS Provider Runtime :9002/v1/tts

Optional CosyVoice sidecar :9012
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

`setup-media-models.sh` 会复用 `scripts/sync-all.sh`，同时准备 Sherpa ASR/TTS 与 Kokoro 模型/voice 文件。只需要重新同步 Python 开发依赖时可运行：

```bash
bash scripts/sync-all.sh
```

配置约定：

```text
config.yaml   非敏感运行配置
.env          API Key / Token
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

Browser Smoke 在 CI 的独立 job 中安装 Playwright/Chromium，不放入默认 `all` extra。

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

- [Architecture](docs/current/ARCHITECTURE.md)
- [Product Design](docs/current/DESIGN.md)
- [Codebase Layout](docs/current/CODEBASE_LAYOUT.md)
- [Person Runtime](docs/current/PERSON_RUNTIME.md)
- [Conversation Runtime](docs/current/CONVERSATION_RUNTIME.md)
- [Memory](docs/current/MEMORY.md)
- [Visual Capture](docs/current/VISUAL_CAPTURE.md)
- [Visual Generation](docs/current/VISUAL_GENERATION.md)
- [Stickers](docs/current/STICKERS.md)
- [Media Runtime](docs/current/MEDIA_RUNTIME.md)
- [Dev Console](docs/current/DEV_CONSOLE.md)
- [Settings Center](docs/current/SETTINGS_CENTER.md)
- [TTS Provider Lab](docs/current/TTS_PROVIDER_LAB.md)
- [Avatar Search](docs/current/AVATAR_SEARCH.md)
- [Evals](docs/current/EVALS.md)

`docs/archive/milestones/` 保存 P0.x 历史交付说明，用于理解演进过程，但其中“当前不做”“当前入口”“轮询频率”等描述可能已经被后续版本取代，不能覆盖源码和 `docs/current/`。

## 核心工程原则

1. **Event 是事实源。** Memory、Mental State、Diary、Trace 都是派生层。
2. **人物可以沉默。** 用户输入不意味着必须回复。
3. **辅助认知失败不应轻易吞掉有效主回复。** 但 outward action 本身仍需要明确合法。
4. **群聊事实只保存一次。** 不把同一房间消息复制成多个彼此独立的“事实”。
5. **多模态仍是同一个人。** Voice/Vision/Capture/ImageGen 都不得绕过 PersonRuntime 另建人格状态。
6. **慢能力隔离。** ASR/TTS、ImageGen 等不能因为失败而破坏已经成立的文本主链路。
7. **先测量再复杂化。** 不因为“以后可能需要”提前引入 Redis、Celery、向量数据库、LangGraph 或大型前端框架。
8. **历史文档不是当前 contract。** 当前 HEAD 与 `docs/current/` 优先。
