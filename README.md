# character_memory

一个用于研究 **Persistent AI Person / 持久化 AI 人物** 的 V0 原型。

当前目标不是做完整社交产品，而是先验证：一个 AI 人物能否在长期交互中保持同一人格，拥有可追溯经历、选择性记忆、持续心理状态、回复/沉默/主动联系等行为，并在虚拟时间中继续生活。

## 当前实现

- SQLite 单文件持久化。
- Append-only Event Log：原始经历是事实源。
- 从第一天启用 Embedding，向量以 `float32 BLOB` 存入 SQLite。
- Vector Recall baseline：semantic similarity + recency + importance。
- 语言形式的 Mental State，不使用 RPG 式好感度/情绪数值作为核心状态。
- `PersonRuntime.handle(event)`：用户消息、时间事件、主动意图走同一条认知/行为链。
- Action：`REPLY`、`MINIMAL_RESPONSE`、`NO_REPLY`、`DEFER`、`PROACTIVE_MESSAGE`、`NO_ACTION`。
- OpenAI-compatible `/chat/completions` 适配器，默认可直接接 OpenCode Go 的 `deepseek-v4-flash`。
- 结构化 LLM 输出校验，非法 JSON/字段会自动重试一次。
- Persistent World Time、Time Tick、Pending Intent。
- Daily Life、Life Event、Social Post 文本、Diary。
- 30-day 虚拟时间模拟。
- CLI、最小 HTTP API、只读 Streamlit Inspector。
- JSONL Eval regression harness 与离线单元测试。

## 最快启动

要求 Python 3.12+。

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[local-embedding,dev]"
character-memory init
$env:OPENCODE_GO_API_KEY="YOUR_KEY"
```

默认配置已经指向：

```yaml
base_url: "https://opencode.ai/zen/go/v1"
chat_model: "deepseek-v4-flash"
embedding_provider: "sentence-transformers"
embedding_model: "BAAI/bge-small-zh-v1.5"
```

OpenCode Go 当前官方文档将 `deepseek-v4-flash` 列在 `/v1/chat/completions` 端点，因此本项目直接使用 OpenAI-compatible adapter。模型列表会变化，实际使用前可运行 `character-memory doctor --remote` 检查。

官方文档：<https://dev.opencode.ai/docs/go/>

### 自检

```powershell
character-memory doctor
character-memory doctor --remote
```

第一次运行本地 BGE embedding 时会下载模型，之后走本地缓存。

### 聊天

```powershell
character-memory chat "今天工作终于结束了"
character-memory chat "到家了"
```

输出会包含开发者可检查的：action、简短 perception/reaction、安全的 mental state、Recall 到的 Memory。这里不是暴露模型隐藏 chain-of-thought。

### 时间与人生

```powershell
character-memory tick
character-memory day
character-memory simulate 30
character-memory inspect
```

`day` / `simulate` 使用持久化 World Time，而不是每次重新从系统日期开始。每天会生成少量 Life Event，在若干时间点触发 Time Tick，最后形成 Diary。

### 修改 Embedding 模型后重建向量

```powershell
character-memory reembed
```

Event Log 和 Memory 文本不变，只重算向量索引层。

## HTTP API

```powershell
pip install -e ".[local-embedding,api]"
character-memory serve
```

主要端点：

- `GET /health`
- `POST /v1/chat`
- `POST /v1/simulate`
- `GET /v1/state/{character_id}`

## Inspector

```powershell
pip install -e ".[ui]"
character-memory inspector
```

当前 Inspector 只做研究观察：World Time、Mental State、Timeline、Memory、Intent。产品 UI 不属于 V0 目标。

## Eval

```powershell
character-memory eval evals/smoke.jsonl
pytest -q
```

Eval 使用隔离的临时 SQLite，避免污染真实人物历史。

## 文档

- [`docs/DESIGN.md`](docs/DESIGN.md)：产品与 Persistent Person 的已确认原则。
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：V0 工程结构、数据流和边界。
- [`docs/MEMORY.md`](docs/MEMORY.md)：语言记忆、Embedding、Recall 与事实源规则。
- [`docs/PERSON_RUNTIME.md`](docs/PERSON_RUNTIME.md)：Reaction、Mental State、Action、Silence、Intent。
- [`docs/EVALS.md`](docs/EVALS.md)：评测目标和 30-day continuity test。
- [`docs/RESEARCH.md`](docs/RESEARCH.md)：已经确认有参考价值的论文/系统，以及我们只借鉴什么。

## 当前明确不做

V0 不引入 LangChain/LangGraph、Redis、Celery、PostgreSQL、Knowledge Graph、复杂 Emotion 数值系统、Voice/TTS、Avatar、真正图片生成、完整 Feed、多用户生产架构。

这些能力只有在 Eval 证明当前简单架构存在真实瓶颈时再加入。
