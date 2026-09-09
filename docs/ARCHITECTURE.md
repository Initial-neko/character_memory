# V0 Architecture

本文只描述当前代码结构与工程边界。

## 1. 技术栈

- Python 3.12+
- Pydantic v2
- httpx
- SQLite
- NumPy
- Sentence Transformers（默认本地 Embedding）
- FastAPI / Uvicorn
- 原生 HTML / CSS / JavaScript
- Streamlit（Developer Inspector）
- pytest

V0 不使用 ORM。

## 2. 当前主路径

正常聊天只有一条应用路径：

```text
HTML / JS
    ↓
FastAPI
    ↓
ChatService
    ↓
PersonRuntime
    ├── Relationship Time
    ├── Vector Recall
    ├── Person Model
    ├── Mental State
    ├── actions[0..3]
    └── Memory Admission
    ↓
SQLite
```

CLI `chat` 也使用 `ChatService`。

UI、API、CLI 不应该各自决定当前聊天时间、conversation/provider session、Runtime 调用顺序、World Time 写入或并发 turn 顺序；这些属于 Application Layer。

## 3. 模块边界

```text
src/character_memory/
  application/
    clock.py          RealClock / FixedClock
    chat_service.py   conversational turn application service
  domain/             Pydantic domain models
  storage/            SQLite source of truth
  memory/             Embedding + VectorRecall
  runtime/            Context Compiler + PersonRuntime
  life/               virtual-time life simulation (frozen)
  llm/                provider adapter
  eval/               regression runner
  web/                HTML / CSS / JS
  api.py              HTTP application surface
  ui.py               read-only Developer Inspector
  cli.py              local entry point
```

`PersonRuntime` 不知道 FastAPI、HTML、Streamlit、OpenCode endpoint 或具体 SQLite UI。

## 4. Clock / Relationship Time

产品聊天：`RealClock -> event_time = 当前现实时间`。

开发/测试可以注入 `FixedClock`。长期模拟仍由 `DayRunner` 操作 persistent world time。

PersonRuntime 在处理新 Event 前，只读取 `event_time <= 当前事件时间` 的最近 USER/CHARACTER chat event，用于构造 Relationship Time。它和 Recall 一样具有 future barrier，因此研究模式回放过去时间时不会看到未来聊天。

Relationship Time 只是 Context：人物自己决定是否提旧事，不存在 `gap > N -> 固定问候` 的模板逻辑。

## 5. Conversation / Provider Session

应用层向 Event metadata 写入 `conversation_id`。PersonRuntime 将 conversation ID 交给 Person Model。

OpenCode adapter：

```text
conversation_id
    ↓
stable UUID
    ↓
x-opencode-session
```

同一 conversation 的 retry / 后续 turn 使用稳定 session。

## 6. PersonReaction / Actions

当前主 contract：

```text
PersonReaction
├── perception              optional safe summary
├── reaction                optional safe summary
├── mental_state_update     optional; empty = keep current state
├── actions[0..3]
├── memory_candidates[]
└── intent_candidates[]
```

可见 Action 当前使用：

- `MESSAGE`
- `EMOJI`

`actions=[]` 是明确的真实沉默。

为兼容冻结子系统与旧 Trace，domain model 仍提供 legacy `action` view：多 Action 时映射首个 Action；明确空 actions 时映射为兼容 `NO_REPLY`。模型必须显式返回 `actions`（包括空数组）或旧 `action`；裸 `{}` 不会被误判成沉默，会 validation fail / retry。

## 7. SQLite

当前表：`events`、`memories`、`mental_states`、`intents`、`world_states`、`runtime_traces`。

SQLite connection 使用进程内可重入锁保护，使 FastAPI sync endpoint 在线程池中使用同一 Store 时不会直接发生跨线程 connection 错误。

V0 仍是单进程、单 SQLite 文件，不宣称支持多进程生产并发。

## 8. Turn Transaction

Source Event 必须先持久化，它是事实源，即使 Provider 失败也不能消失。

Provider 成功并得到合法 `PersonReaction` 后，先执行 Memory admission / 必要 embedding，再开启 derived-state transaction：

```text
BEGIN
  Mental State
  Accepted Memory
  Intent
  0..3 Character Messages
  Runtime Trace
  ACTION Event
COMMIT
```

任何派生写入异常则 `ROLLBACK`。允许 Raw Event 存在但 Derived processing failed，不允许 Mental State/Memory/ACTION 只写一半。

## 9. Memory Admission

Memory Candidate 不直接等于 Memory Write。

当前 deterministic baseline：

```text
importance < 0.35       -> SKIP_LOW_VALUE
exact duplicate         -> SKIP_DUPLICATE
embedding cosine >= .93 -> SKIP_DUPLICATE
otherwise               -> WRITE
```

每个 decision 进入 Runtime Trace。Admission 不增加第二个 LLM 调用。

## 10. Runtime Trace / Safe Thought

Trace 是 Debug/Research 数据，不属于 Event 本体，因此使用独立 `runtime_traces`。

Trace 包含 source Event、Compiled Context、Relationship Time source、实际 model messages、raw structured response、Recall snapshot、developer-safe Perception/Reaction、Mental State before/after、Actions、Memory admission、Intent candidate 与 created IDs。

WebUI 的 `想法` 只展示 `perception / reaction` 两个安全摘要，不展示 raw hidden chain-of-thought。完整 Trace 继续通过 `···` Developer Detail 查看。

聊天列表只查询 USER/CHARACTER message；点击详情时按 `source_event_id` 单独读取 Trace。

## 11. Provider

`OpenAICompatibleModel` 持有长期 `httpx.Client`，复用 connection pool / keep-alive / TLS。

结构化调用使用 `response_format=json_object`，Pydantic 负责业务 contract validation。结构化调用当前串行化，避免并发请求互相覆盖 `last_request/response` Trace 状态。

Provider 错误保留安全的 response body / request ID 到日志，但禁止打印 API Key。

## 12. Web 与 Inspector

HTML/JS Web 是正式 V0 人工聊天入口，负责 Character 切换、per-character pending、0~3 条消息渲染、真实沉默提示、Safe Thought、HTTP 调用以及右侧按需 Trace/Runtime drawer，不直接访问 SQLite/Runtime。

Streamlit Inspector 是 Developer-only、只读，负责 State / Chat / Memory / Intent / Trace，不加载 Person Model / Sentence Transformers。

## 13. Eval

`EvalRunner` 支持单 Runtime 或多 Character Runtime map。

`evals/p0_relationship.jsonl` 当前是 24-case P0 suite，覆盖：

- Persona expression contract
- questions / disagreement
- explicit silence
- Memory precision / write
- Recall
- 7-day re-encounter
- safe summary

Judge Model 暂未进入 Runtime 主链路。

## 14. Life Simulation

Life / Diary / TimeTicker 保留现有实现用于研究，但不是当前 P0 扩展目标。新 `MESSAGE / EMOJI` 保留旧 Intent lifecycle 兼容，不继续扩展 Life subsystem。

## 15. 当前故意不引入

- External Information / Web Tool Agent（本轮不做）
- LangChain / LangGraph
- Redis / Celery
- PostgreSQL / pgvector
- Knowledge Graph
- 微服务
- React / Next.js
- Voice / TTS / Full-duplex
- Avatar / Video
- 多用户生产级架构

当前 HTML/JS 保持无前端构建链，先验证交互与 Person Engine。
