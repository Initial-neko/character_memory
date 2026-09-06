# V0 Architecture

本文只描述当前代码结构与工程边界。

## 1. 技术栈

- Python 3.12+
- Pydantic v2
- httpx
- SQLite
- NumPy
- Sentence Transformers（默认本地 Embedding，可选）
- FastAPI/Uvicorn（可选 HTTP API）
- Streamlit（Research Console）
- pytest

V0 不使用 ORM；SQLite schema 很小，直接使用标准库 `sqlite3`。

## 2. 主数据流

对话/事件：

`Event -> SQLite Event Log -> Vector Recall -> Context Compiler -> Person Model -> Mental State -> Action -> Event/Memory/Intent`

时间/生活：

`Persistent World Time -> Life Plan -> Life Events -> Time Tick/Intent -> Diary -> Memory/Mental State -> Next Day`

## 3. 核心模块

```text
src/character_memory/
  domain/        Pydantic domain models
  storage/       SQLite source of truth
  memory/        Embedding + VectorRecall
  runtime/       Context Compiler + PersonRuntime
  life/          World time, ticker, day runner, life simulator
  llm/           Provider adapter
  eval/          Regression runner
  api.py         Optional HTTP surface
  ui.py          Interactive research console
  cli.py         Local entry point
```

Provider 和存储实现不能进入人物认知逻辑：`PersonRuntime` 不应该知道 OpenCode、Sentence Transformers、Streamlit 等具体产品。

## 4. SQLite tables

当前只有五类持久化状态：

- `events`：append-only 原始事件。
- `memories`：语言 Memory + embedding BLOB + provenance。
- `mental_states`：每个角色一份紧凑当前心理状态。
- `intents`：未来可能执行的行为意图。
- `world_states`：角色当前 Runtime 时间。

数据库是单文件，方便复制成独立实验：

```text
data/
  character-memory.db
runs/
  persona-v1.db
  recall-v2.db
  rin-30day.db
```

## 5. Provider boundary

Person Model：OpenAI-compatible `/chat/completions` adapter。

Embedding：

- `SentenceTransformerEmbedding`
- `OpenAICompatibleEmbedding`
- `DeterministicEmbedding` 仅用于离线测试

Embedding 模型不是 Person Model 的附属能力，两者独立配置。

## 6. Research Console 与 Runtime Trace

V0 的主要人工评估入口是 `character-memory inspector`。它不是产品 UI，而是为了回答：人物这次为什么这么做、到底给模型提供了什么。

每一次 `PersonRuntime.handle(event)` 都会把一份开发者 Trace 写进对应的 `ACTION` Event metadata。Trace 当前包含：

- source event 与时间；
- Compiled Context；
- 实际发送给 OpenAI-compatible model 的 messages；
- raw structured model response；
- Recall 到的 Memory snapshot；
- developer-safe Perception / Reaction；
- Mental State before / after；
- Action、Action Reason 和最终对外 message；
- Memory Candidate / created memory IDs；
- Intent Candidate / created intent IDs。

这样历史轮次也可以重放检查，而不是只看到当前状态。

这里故意不保存或展示模型隐藏 chain-of-thought；`Perception / Reaction / Action Reason` 是 Prompt 明确要求的简短、安全结构化摘要。

Research Console 同时允许：聊天、同步现实时间、推进小时/天、批量时间模拟，以及查看 Timeline、Memory、Persona、Mental State、Intent。

## 7. 当前故意不引入

- LangChain / LangGraph
- Redis / Celery
- PostgreSQL / pgvector
- Knowledge Graph
- 微服务
- Voice / TTS / Full-duplex
- Avatar / Video
- 真正图片生成
- 多用户生产级权限和扩缩容

原则：先通过 Eval 发现真实瓶颈，再增加复杂度。
