# character_memory

一个用于研究 **Persistent AI Person / 持久化 AI 人物** 的 V0 原型。

当前阶段只做 **Phase 1 — Prove the Person**：验证同一个 AI 人物能否在长期交互中保持人格、拥有可追溯经历、选择性记忆、持续心理状态，并自然地回复、沉默、延后或主动联系。

## 当前实现

- SQLite 单文件持久化。
- Append-only Event Log：原始经历是事实源。
- Embedding + SQLite `float32 BLOB` + Vector Recall。
- 语言形式 Mental State。
- `PersonRuntime.handle(event)` 统一处理用户消息、Time Tick、Intent。
- Action：`REPLY` / `MINIMAL_RESPONSE` / `NO_REPLY` / `DEFER` / `PROACTIVE_MESSAGE` / `NO_ACTION`。
- OpenAI-compatible Person Model；默认 OpenCode Go `deepseek-v4-flash`。
- OpenCode Go conversation session header 自动处理。
- Persistent World Time、Life Event、Diary、Pending Intent、时间模拟。
- Runtime Trace 独立持久化，可按单轮回看 Context / Recall / Reaction / Action / Memory / Intent。
- FastAPI + 原生 HTML/CSS/JS 聊天 WebUI。
- Streamlit Developer Inspector。
- 结构化后端日志。
- JSONL Eval regression harness 与 pytest。

## V0.4.1 收敛后的运行结构

正常聊天统一经过：

```text
HTML / JS
    ↓
FastAPI
    ↓
ChatService
    ├── RealClock
    ├── conversation_id
    └── per-character turn lock
    ↓
PersonRuntime
    ↓
Recall / Person Model / Mental State / Action
    ↓
SQLite
```

CLI 的 `chat` 也调用同一个 `ChatService`。

Streamlit 不再承担正式聊天交互，只保留为 Developer Inspector。

## 安装

要求 Python 3.12+。

推荐：

```powershell
git clone https://github.com/Initial-neko/character_memory.git
cd character_memory
uv sync --extra all
uv run character-memory init
$env:OPENCODE_GO_API_KEY="YOUR_KEY"
```

默认：

```yaml
base_url: "https://opencode.ai/zen/go/v1"
chat_model: "deepseek-v4-flash"
embedding_provider: "sentence-transformers"
embedding_model: "BAAI/bge-small-zh-v1.5"
db_path: "data/character-memory.db"
```

第一次加载本地 BGE embedding 时会下载模型。

## 主要入口：HTML / JS WebUI

```powershell
uv run character-memory web
```

打开：

```text
http://127.0.0.1:8000
```

页面支持：

- 正常聊天；
- Enter 发送、Shift+Enter 换行；
- 非流式等待时显示“正在输入中”；
- 聊天历史与时间分隔；
- 每条有 Trace 的消息通过 `···` 打开右侧详情；
- 查看 Perception / Reaction / Action Reason；
- 查看 Mental State Before / After；
- 查看本轮 Recall；
- 查看实际发送给模型的 messages；
- 查看 Compiled Context；
- 查看 Memory / Intent Write；
- 查看 Raw Model Response；
- 顶部 `Runtime` 按钮按需查看 Persona、Mental State、Memory、Intent、Provider。

前端不直接操作 Runtime/SQLite，只调用 FastAPI。

## Developer Inspector

```powershell
uv run character-memory inspector
```

Inspector 是只读研究工具，主要用于 State、Chat、Memory、Intent、Trace。它不会加载 Sentence Transformers 或 Person Model，因此不再承担聊天时的重型 Runtime 初始化。

## CLI

```powershell
uv run character-memory doctor
uv run character-memory doctor --remote
uv run character-memory chat "今天工作终于结束了"
uv run character-memory tick
uv run character-memory day
uv run character-memory simulate 7
uv run character-memory inspect
```

`chat` 默认使用现实时间；模拟命令继续使用 persistent simulated world time。

## Runtime Trace

Trace 不再放进 `ACTION.metadata_json`。

当前数据库中：

```text
events
memories
mental_states
intents
world_states
runtime_traces
```

旧版本 `ACTION.metadata.trace` 会在数据库初始化时自动迁移到 `runtime_traces` 并从 Event metadata 中移除。

正常聊天历史只查询 `USER_MESSAGE / CHARACTER_MESSAGE`；只有点击详情时才读取对应 Trace，避免每次页面刷新解析大量 Context / Prompt / Raw Response。

## 一轮写入一致性

原始用户 Event 会先持久化，因为它是 Source of Truth。

LLM 成功后，以下派生状态在一个 SQLite transaction 中提交：

```text
Mental State
Memory
Intent
Character Message
Runtime Trace
ACTION Event
```

如果派生写入失败，则整组 rollback，避免出现“状态改了一半、Trace 又没有”的半轮数据。

## Provider

OpenCode Go inference 会携带 `x-opencode-session` / `x-opencode-client` / `User-Agent`。

Web 前端将 `conversation_id` 保存在浏览器 `localStorage`；Provider adapter 会将 conversation ID 稳定映射为 UUID。

`httpx.Client` 在 Model 生命周期内复用，不再每次请求重新建立连接。

## 后端日志

Web/API 默认输出 `INFO` 日志：

```text
API → ChatService → Runtime Event → Recall → Context → Provider → Action → Persist
```

Provider HTTP error body 会直接输出，但不会输出 API Key。

更细日志：

```powershell
$env:CHARACTER_MEMORY_LOG_LEVEL="DEBUG"
uv run character-memory web
```

## Eval / Test

```powershell
uv run pytest -q
uv run character-memory eval evals/smoke.jsonl
```

当前 Eval 仍只是 regression harness；长期 Persona / Memory / 30-day continuity 评测见 `docs/EVALS.md`。

## 文档职责

- `docs/DESIGN.md`：产品与 Persistent Person 已确认原则。
- `docs/ARCHITECTURE.md`：当前工程边界与数据流。
- `docs/MEMORY.md`：Memory / Embedding / Recall 原则。
- `docs/PERSON_RUNTIME.md`：Reaction / Mental State / Action / Silence / Intent。
- `docs/EVALS.md`：评测计划。
- `docs/RESEARCH.md`：外部研究参考。

## 当前明确不做

V0 不引入 LangChain/LangGraph、Redis、Celery、PostgreSQL、Knowledge Graph、复杂 Emotion 数值系统、Voice/TTS、Avatar、Video、完整 Feed、多用户生产架构。

先把 **真实聊天 vertical slice** 跑稳，再继续扩展人物能力。
