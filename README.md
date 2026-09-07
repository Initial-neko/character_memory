# character_memory

一个用于研究 **Persistent AI Person / 持久化 AI 人物** 的 V0 原型。

当前目标不是做完整社交产品，而是先验证：一个 AI 人物能否在长期交互中保持同一人格，拥有可追溯经历、选择性记忆、持续心理状态、回复/沉默/主动联系等行为，并在时间中继续生活。

## 当前实现

- SQLite 单文件持久化。
- Append-only Event Log：原始经历是事实源。
- 从第一天启用 Embedding，向量以 `float32 BLOB` 存入 SQLite。
- Vector Recall baseline：semantic similarity + recency + importance。
- 语言形式的 Mental State，不使用 RPG 式好感度/情绪数值作为核心状态。
- `PersonRuntime.handle(event)`：用户消息、时间事件、主动意图走同一条认知/行为链。
- Action：`REPLY`、`MINIMAL_RESPONSE`、`NO_REPLY`、`DEFER`、`PROACTIVE_MESSAGE`、`NO_ACTION`。
- OpenAI-compatible `/chat/completions` 适配器，默认可接 OpenCode Go 的 `deepseek-v4-flash`。
- 结构化 LLM 输出校验，非法 JSON/字段自动重试一次。
- Persistent World Time、Time Tick、Pending Intent。
- Daily Life、Life Event、Social Post 文本、Diary。
- 30-day 虚拟时间模拟。
- 交互式 Streamlit Research Console：聊天、时间推进、Runtime Trace、Memory、Timeline、Intent。
- 每轮 Runtime Trace 持久化：可回看实际送给模型的 messages、Recall、Perception、Reaction、Mental State 变化、Action、Memory Write、Intent。
- Research Console 后端结构化日志：启动、Embedding、Provider、Recall、Context、Action、Memory/Intent 与错误链路都会输出到终端。
- JSONL Eval regression harness 与离线单元测试。

## 推荐启动方式：uv

要求 Python 3.12+。

```powershell
git clone https://github.com/Initial-neko/character_memory.git
cd character_memory

uv sync --extra all
uv run character-memory init
$env:OPENCODE_GO_API_KEY="YOUR_KEY"
```

默认配置：

```yaml
base_url: "https://opencode.ai/zen/go/v1"
chat_model: "deepseek-v4-flash"
embedding_provider: "sentence-transformers"
embedding_model: "BAAI/bge-small-zh-v1.5"
db_path: "data/character-memory.db"
```

第一次加载本地 BGE embedding 时会下载模型，之后走本地缓存。

## 先启动 Research Console

这是当前推荐的主要测试入口，不再建议长期依赖 CLI 观察人物行为。

```powershell
uv run character-memory inspector
```

打开页面后可以直接：

- 和角色聊天；
- 查看消息对应的现实时间；
- 生成回复时显示“正在输入中…”，完整返回后一次性展示；
- 点击具体消息的 `···` 后，按需弹出该轮完整 Runtime Trace；
- 点击顶部 `Runtime` 按钮后，按需查看 Provider、Persona、Mental State、Memory、Intent；
- 查看每轮真正发给用户的消息，或 `NO_REPLY / DEFER`；
- 查看开发者安全的 `Perception / Reaction / Mental State / Action Reason`；
- 查看本轮 Recall 到哪些 Memory；
- 查看 **实际发送给模型的 system/user messages**；
- 查看 Runtime 生成的 Compiled Context；
- 查看 Raw Structured Model Response；
- 查看 Memory Candidate、真正写入的 Memory ID、Intent Candidate 和 Intent ID。

这里展示的“内心活动”是系统专门要求模型输出的简短开发者安全摘要，不是模型隐藏 chain-of-thought。

### 后端运行日志

`character-memory inspector` 默认在启动它的终端输出 `INFO` 级别运行日志，包括：

- WebUI session 启动与 Runtime 初始化；
- Embedding / Person Model 加载耗时；
- 用户消息进入 Runtime；
- Event 写入、Recall 数量与耗时、Context 长度；
- Provider 请求开始/完成、HTTP 状态、耗时、输入/输出字符数；
- Action、Memory Write、Intent Write；
- Provider error body 与异常 stack trace。

不会输出 API Key，也不会默认把完整 Prompt / Persona / Memory 内容刷到终端。

需要更细日志时：

```powershell
$env:CHARACTER_MEMORY_LOG_LEVEL="DEBUG"
character-memory inspector
```

## CLI 仍保留用于 smoke test / 自动化

```powershell
uv run character-memory doctor
uv run character-memory doctor --remote
uv run character-memory chat "今天工作终于结束了"
uv run character-memory tick
uv run character-memory day
uv run character-memory simulate 7
uv run character-memory inspect
```

## 修改 Embedding 模型后重建向量

```powershell
uv run character-memory reembed
```

Event Log 和 Memory 文本不变，只重算向量层。

## HTTP API

```powershell
uv run character-memory serve
```

主要端点：

- `GET /health`
- `POST /v1/chat`
- `POST /v1/simulate`
- `GET /v1/state/{character_id}`

## Eval

```powershell
uv run character-memory eval evals/smoke.jsonl
uv run pytest -q
```

Eval 使用隔离的临时 SQLite，避免污染真实人物历史。

## 文档职责

- [`docs/DESIGN.md`](docs/DESIGN.md)：产品与 Persistent Person 的已确认原则。
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：V0 工程结构、数据流、Research Console 和边界。
- [`docs/MEMORY.md`](docs/MEMORY.md)：语言记忆、Embedding、Recall 与事实源规则。
- [`docs/PERSON_RUNTIME.md`](docs/PERSON_RUNTIME.md)：Reaction、Mental State、Action、Silence、Intent。
- [`docs/EVALS.md`](docs/EVALS.md)：评测目标和 30-day continuity test。
- [`docs/RESEARCH.md`](docs/RESEARCH.md)：已经确认有参考价值的论文/系统，以及我们只借鉴什么。

## 当前明确不做

V0 不引入 LangChain/LangGraph、Redis、Celery、PostgreSQL、Knowledge Graph、复杂 Emotion 数值系统、Voice/TTS、Avatar、真正图片生成、完整 Feed、多用户生产架构。

这些能力只有在 Eval 证明当前简单架构存在真实瓶颈时再加入。
