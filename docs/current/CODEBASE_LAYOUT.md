# Codebase Layout

这份文档回答一个问题：**我要改某个能力，应该从哪里开始看？**

仓库根目录保持标准 Python 项目结构，不把 `src/`、`tests/`、`personas/` 等再套一层无意义目录。真正需要导航的是 `src/character_memory/` 内部职责。

## Root

```text
.
├─ src/character_memory/    Python package
├─ personas/                character definitions
├─ docs/                    current / research / archive
├─ tests/                   regression tests
├─ evals/                   long-term behavior eval data
├─ scripts/                 dev/media utility scripts
├─ .github/workflows/       CI
├─ config.example.yaml
└─ pyproject.toml
```

## Core runtime packages

```text
src/character_memory/
├─ application/             application orchestration
├─ domain/                  Pydantic domain contracts
├─ runtime/                 PersonRuntime + context/resource selection
├─ memory/                  embedding + recall
├─ storage/                 SQLite core persistence/search/history
├─ llm/                     cloud model adapter
├─ life/                    frozen life/time simulation
└─ eval/                    eval runner
```

### `application/`

- `chat_service.py` — direct application service / per-character lock。
- `async_conversation.py` — ReactionScheduler、watermark、SSE hub。
- `group_conversation_service.py` — group member ordering、shared-room reaction、member fault isolation。
- `proactive_service.py` — persisted Intent execution。
- `wake_service.py` — process-local character wake opportunity。
- `clock.py` — RealClock / FixedClock。

### `domain/`

- `models.py` — `Event / Memory / PersonReaction / ActionDecision / ...`。

这里是 LLM structured-output 的主要 contract 边界。修改 action 语义时优先从这里看，而不是只改 Prompt。

### `runtime/`

- `person_runtime.py` — Event → Recall → LLM → derived persistence。
- `context.py` — compiled character context。
- `sticker_retrieval.py` — Sticker 候选检索。

### `memory/`

- `embedding.py` — deterministic / sentence-transformers / OpenAI-compatible embedding。
- `recall.py` — VectorRecall ranking。

### `storage/`

- `sqlite.py` — core tables、migration ledger、Event/Memory/Trace/Intent/Media persistence。
- `chat_history.py` — direct history read model。
- `message_search.py` — direct/group durable message search repository helpers。

## HTTP / Web route modules

当前有一些 `*_web.py` 留在 package 顶层。这是历史演进留下的薄 route layer，**不要再继续把新的大型 domain/runtime 逻辑塞进这些文件**。

```text
api.py             core FastAPI app + direct/common APIs
server.py          application assembly / attach routes
async_web.py       async message accept + SSE routes
group_web.py       group HTTP surface
history_web.py     history APIs
search_web.py      message search APIs
avatar_web.py      avatar/search HTTP surface
visual_web.py      ImageGen/visual tool HTTP surface
wake_web.py        wake/debug HTTP surface
```

`server.py` 是查看 Character Runtime 当前装配顺序的最快入口。

如果未来 route 数量继续明显增长，可以单独做一次低风险迁移，把这些薄文件移动到 `web_routes/` 或 `transport/http/`；本轮不为了目录美观制造全仓 import churn。

## Feature modules currently at package root

这些文件仍在顶层，但各自有明确 ownership：

```text
avatars.py              avatar local persistence
avatar_intent.py        AI avatar-search planning
images.py               character image catalog
stickers.py             global/legacy sticker catalog
search.py               external avatar image search providers
visual_generation.py    ImageGen providers + prompt compiler
visual_runtime.py       autonomous generated-image execution
media.py                media asset storage/contracts
media_runtime.py        local ASR/TTS providers/runtime
media_bootstrap.py      Windows/native media bootstrap
media_server.py         Media Runtime FastAPI
persona_builder.py      character draft/build flow
resource_metrics.py     local resource sampling
config.py               Settings + character discovery
logging_utils.py        logging setup
time_utils.py           datetime helpers
```

## Entrypoints

`pyproject.toml`：

```text
character-memory -> character_memory.cli:main
character-media  -> character_memory.media_bootstrap:main
character-dev    -> character_memory.dev_server:main
character-stack  -> character_memory.dev_stack:main
```

开发日常优先：

```bash
bash scripts/sync-all.sh
uv run character-stack
```

## Frontend

```text
src/character_memory/web/
```

当前正式 UI 没有 bundler。JS 按 feature 拆分，HTML 直接按顺序加载。

重要原则：

- `app.js` 是 conversation/composer 的核心 owner；feature module 不重复注册相互竞争的主 submit handler。
- `images.js` 管普通图片 draft；`ai_images.js` 只负责生成来源，最终复用同一 image draft/send path。
- `groups.js` 管 group-specific UX，但 durable group facts 仍由后端 `conversation_events` 管理。
- 历史 `p0_*.css` 是待整理样式债务，不代表后端 architecture package。

## Tests

`tests/` 暂时保持单层文件名，因为很多 test fixture 使用 repository-root 相对路径，同时 CI 已经稳定运行。

不要仅为了“看起来分文件夹”一次性移动几十个测试文件；更合理的后续规则是：

- 新测试优先按 feature 取清楚名字；
- 当某 feature tests 达到足够规模时，再连同 fixture/root-path helper 一起迁移子目录；
- CI 必须在目录迁移前后保持同样的 test discovery 与 browser marker 行为。

## 修改功能时的最短阅读路径

| 目标 | 建议先看 |
| --- | --- |
| LLM 回复/structured output | `domain/models.py` → `llm/client.py` → `runtime/person_runtime.py` |
| Direct async/SSE | `application/async_conversation.py` → `async_web.py` → `web/app.js` |
| Group chat | `group_store.py` → `application/group_conversation_service.py` → `group_web.py` → `web/groups.js` |
| Memory/Recall | `memory/embedding.py` → `memory/recall.py` → `storage/sqlite.py` |
| Sticker | `stickers.py` → `runtime/sticker_retrieval.py` → `web/stickers.js` |
| 用户图片/Vision | `media.py` → `api.py/async_web.py` → `web/images.js` |
| ImageGen | `visual_generation.py` → `visual_runtime.py` → `visual_web.py` → `web/ai_images.js` |
| Avatar | `avatars.py` / `avatar_intent.py` → `avatar_web.py` → `web/avatars.js` |
| Voice | `media_bootstrap.py` → `media_runtime.py` → `media_server.py` → `web/voice.js` |
| Dev Console | `dev_server.py` → `web/dev*.{html,js,css}` |
