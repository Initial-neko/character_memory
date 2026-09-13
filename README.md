# character_memory

`character_memory` 是一个用于研究 **Persistent AI Person / 持久化 AI 人物** 的实验项目。

项目当前仍以 **Prove the Person** 为核心：验证一个人物能否在长期互动中保持可辨识的人格、拥有可追溯经历、选择性记忆、持续心理状态与自主表达，并让今天的行为能够被过去解释。

语音、头像、图片生成、群聊等能力已经进入仓库，但它们都是人物表达与交互的渠道，不改变这条核心产品判断：

> 不优化“人物有多喜欢用户”，而优化“人物现在为什么会这样做”。

## 当前能力

- 多 Character Persona；`personas/*/persona.yaml` 是人物定义事实源。
- SQLite Event Log、Memory、Mental State、Intent、Runtime Trace。
- 本地 BGE Embedding + Vector Recall；同一进程内所有人物共享 Embedding / LLM Provider。
- `PersonReaction.actions[0..3]`：`MESSAGE / EMOJI / STICKER / IMAGE`，以及内部工具意图 `GENERATE_IMAGE`。
- `actions=[]` 是合法沉默；辅助 Memory/Intent 字段允许安全容错，主 outward action contract 仍严格。
- Direct Chat + Group Chat；群聊共享事实只保存一次，成员按因果顺序逐个判断。
- 异步消息接受：用户消息先持久化并立即返回 202，人物反应通过 SSE 渐进推送。
- Message Search、Group Mentions、Unread、Intent Preview。
- 用户图片输入 + Vision；本地 Sticker / Image Catalog。
- ImageGen：角色自主 `SELFIE / SCENE`，以及用户显式“AI 生成图片”工具；生成后可像粘贴图片一样先进入草稿再手动发送。
- Avatar Search / Avatar Generate / 从聊天图片设头像。
- 独立 Media Runtime：本地 ASR（SenseVoice）+ TTS（VITS）。
- Dev Console：统一测试 LLM、ASR/TTS、ImageGen、资源与运行状态。
- pytest、Browser Smoke、JSONL Eval regression。

## 运行架构

```text
Browser
├─ Chat UI ------------------------------┐
└─ Dev Console :8002                     │
                                         │
Character Runtime :8000                  │
├─ FastAPI routes                        │
├─ async message accept + SSE            │
├─ ReactionScheduler                     │
├─ PersonRuntime                         │
│  ├─ Persona / Relationship Time        │
│  ├─ Memory Recall / Mental State       │
│  ├─ Cloud LLM / Vision                 │
│  └─ MESSAGE / STICKER / IMAGE / ...    │
├─ Visual Runtime / Image Providers      │
└─ SQLite + local media metadata/files   │
                                         │
Media Runtime :8001 <--------------------┘
├─ local ASR
└─ local TTS
```

Character Runtime 与 Media Runtime 是独立进程。Voice 只是同一个 Persistent Person 的另一条输入/输出渠道，不存在第二套“语音人物”。

## 开发环境

要求：

- Python `>=3.12,<3.13`
- 推荐 `uv`
- Windows 开发主路径支持 Git Bash

首次准备完整开发环境：

```bash
git clone https://github.com/Initial-neko/character_memory.git
cd character_memory
bash scripts/sync-all.sh
uv run character-memory init
```

配置本地 `config.yaml`，API Key 建议通过环境变量注入，例如：

```bash
export OPENCODE_GO_API_KEY="..."
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

`character-stack` 会复用已经健康运行的服务。修改 Runtime 代码后如果发现行为仍像旧版本，请先结束旧进程，再重新启动，避免复用旧 `:8000`。

可选：

```bash
uv run character-stack --open chat
uv run character-stack --no-browser
```

## 常用测试

```bash
# Python contract / integration tests
uv run pytest -q

# 完整开发依赖重新同步
bash scripts/sync-all.sh

# Media 本地 benchmark
uv run python scripts/benchmark_media.py --wav path/to/test.wav --iterations 20

# Relationship eval
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
- [Visual Generation](docs/current/VISUAL_GENERATION.md)
- [Media Runtime](docs/current/MEDIA_RUNTIME.md)
- [Dev Console](docs/current/DEV_CONSOLE.md)
- [Avatar Search](docs/current/AVATAR_SEARCH.md)
- [Evals](docs/current/EVALS.md)

`docs/archive/milestones/` 保存 P0.x 历史交付说明，用于理解演进过程，但其中“当前不做”“当前入口”“轮询频率”等描述可能已经被后续版本取代，不能覆盖源码和 `docs/current/`。

## 核心工程原则

1. **Event 是事实源。** Memory、Mental State、Diary、Trace 都是派生层。
2. **人物可以沉默。** 用户输入不意味着必须回复。
3. **辅助认知失败不应轻易吞掉有效主回复。** 但 outward action 本身仍需要明确合法。
4. **群聊事实只保存一次。** 不把同一房间消息复制成多个彼此独立的“事实”。
5. **慢能力隔离。** ASR/TTS、ImageGen 等不能因为失败而破坏已经成立的文本主链路。
6. **先测量再复杂化。** 不因为“以后可能需要”提前引入 Redis、Celery、向量数据库、LangGraph 或大型前端框架。
7. **历史文档不是当前 contract。** 当前 HEAD 与 `docs/current/` 优先。
