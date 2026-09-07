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

正常聊天只允许一条应用路径：

```text
HTML / JS
    ↓
FastAPI
    ↓
ChatService
    ↓
PersonRuntime
    ↓
SQLite / Recall / Person Model
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
  life/               virtual-time life simulation
  llm/                provider adapter
  eval/               regression runner
  web/                HTML / CSS / JS
  api.py              HTTP application surface
  ui.py               read-only Developer Inspector
  cli.py              local entry point
```

`PersonRuntime` 不知道 FastAPI、HTML、Streamlit、OpenCode endpoint 或具体 SQLite UI。

## 4. Clock

产品聊天：`RealClock -> event_time = 当前现实时间`。

开发/测试可以注入 `FixedClock`。长期模拟仍由 `DayRunner` 操作 persistent world time。

因此现实聊天时间和模拟推进工具分开，而不是由 UI 自己覆盖时间。

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

同一 conversation 的 retry / 后续 turn 使用稳定 session；Provider 对象不再把一个随机 Model-instance UUID 当成所有浏览器会话的共同 session。

## 6. SQLite

当前表：`events`、`memories`、`mental_states`、`intents`、`world_states`、`runtime_traces`。

SQLite connection 使用进程内可重入锁保护，使 FastAPI sync endpoint 在线程池中使用同一 Store 时不会直接发生跨线程 connection 错误。

V0 仍是单进程、单 SQLite 文件，不宣称支持多进程生产并发。

## 7. Turn Transaction

Source Event 必须先持久化，它是事实源，即使 Provider 失败也不能消失。

Provider 成功并得到合法 `PersonReaction` 后，先计算所有 Memory embedding，再开启 derived-state transaction：

```text
BEGIN
  Mental State
  Memory
  Intent
  Character Message
  Runtime Trace
  ACTION
COMMIT
```

任何派生写入异常则 `ROLLBACK`。允许 Raw Event 存在但 Derived processing failed，不允许 Mental State/Memory/ACTION 只写一半。

## 8. Runtime Trace

Trace 是 Debug/Research 数据，不属于 Event 本体，因此从 `ACTION.metadata.trace` 拆到 `runtime_traces`。

Trace 包含 source Event、Compiled Context、实际 model messages、raw structured response、Recall snapshot、developer-safe Perception/Reaction、Mental State before/after、Action、Memory/Intent candidate 与 created IDs。

旧数据库会自动迁移历史 `ACTION.metadata.trace`。聊天列表只查询 USER/CHARACTER message；点击详情时按 `source_event_id` 单独读取 Trace。

## 9. Provider

`OpenAICompatibleModel` 持有长期 `httpx.Client`，复用 connection pool / keep-alive / TLS。结构化调用串行化，避免并发请求互相覆盖 `last_request/response` Trace 状态。

Provider 错误保留安全的 response body / request ID 到日志，但禁止打印 API Key。

## 10. Web 与 Inspector

HTML/JS Web 是正式 V0 人工聊天入口，负责消息、输入、typing、HTTP 调用以及右侧按需 Trace/Runtime drawer，不直接访问 SQLite/Runtime。

Streamlit Inspector 是 Developer-only、只读，负责 State / Chat / Memory / Intent / Trace，不加载 Person Model / Sentence Transformers。

## 11. Life Simulation

Life / Diary / TimeTicker 保留现有实现用于研究，但不是当前 Stabilization 的扩展目标。

真实聊天 vertical slice 稳定后，再继续评估 30-day continuity、Proactive precision、Persona decay、Memory policy。

## 12. 当前故意不引入

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
