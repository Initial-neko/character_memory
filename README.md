# character_memory

一个用于研究 **Persistent AI Person / 持久化 AI 人物** 的 V0 原型。

当前阶段只做 **Phase 1 — Prove the Person / Relationship**：验证同一个 AI 人物能否在长期交互中保持人格、拥有可追溯经历、选择性记忆、持续心理状态，并自然地表达、追问、沉默、使用表情包和再次相遇。

## 当前实现

- SQLite 单文件持久化。
- Append-only Event Log：原始经历是事实源。
- Embedding + SQLite `float32 BLOB` + Vector Recall。
- Memory Admission：低价值/近重复 Memory Candidate 可跳过，并记录决策 Trace。
- 语言形式 Mental State；空 update 表示本轮沿用旧状态。
- Relationship Time：Runtime 知道最近一次聊天和距今时间，不写死机械问候。
- `PersonRuntime.handle(event)` 统一处理用户消息、Time Tick、Intent。
- P0 对外行为：`actions[0..3]`，当前支持 `MESSAGE / EMOJI / STICKER`；`actions=[]` 是真正沉默。
- 旧 `REPLY / MINIMAL_RESPONSE / NO_REPLY / DEFER / PROACTIVE_MESSAGE / NO_ACTION` 保留兼容，不再是新聊天主 contract。
- OpenAI-compatible Person Model；默认 OpenCode Go `deepseek-flash`（DeepSeek V4.1 Flash），结构化请求使用 `response_format=json_object` + Pydantic validation。
- OpenCode Go conversation session header 自动处理。
- 多 Character：每个 Character 独立 Persona、聊天历史、Mental State、Memory、conversation session、可选 Sticker Catalog；Embedding / Provider / SQLite 共享。
- Persistent World Time、Life Event、Diary、Pending Intent、时间模拟（当前冻结，不作为 P0 扩展重点）。
- P0.6 主动消息：后台只检查已经存在且到期的 Intent；没有 due Intent 时不调用模型。
- P0.6 未读红点：一次 `/v1/characters/summaries` 请求返回全部人物摘要，前端本地维护 read state。
- P0.7 Intent Preview：可查看 Intent 状态、计划时间、来源 Event，并跳到产生它的 Runtime Trace。
- P0.7 Sticker：用户和人物都能发送本地表情包；人物只能选择 Manifest 中真实存在的 Sticker ID。
- Runtime Trace 独立持久化，可按单轮回看 Context / Recall / Reaction / Actions / Sticker / Memory Admission / Intent / timings。
- FastAPI + 原生 HTML/CSS/JS 聊天 WebUI。
- 用户可选「想法」视图：只展示安全 `perception / reaction` 摘要，不展示 raw chain-of-thought。
- Streamlit Developer Inspector。
- 结构化后端日志与单轮耗时指标。
- JSONL Eval regression harness 与 pytest。

## 当前聊天结构

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
PersonRuntime(character persona)
    ├── Relationship Time
    ├── Vector Recall
    ├── Person Model
    ├── Mental State
    ├── Sticker Catalog
    ├── actions[0..3]
    └── Memory Admission
    ↓
SQLite Event / Memory / Intent / Trace
```

CLI 的 `chat` 也调用同一个 `ChatService`。Streamlit 不再承担正式聊天交互，只保留为 Developer Inspector。

## 安装

要求 Python 3.12+。

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
chat_model: "deepseek-flash"
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

页面采用聊天优先的双栏结构：左侧是 Character 列表，右侧是当前 Character 的聊天。切换 Character 时，历史记录和 conversation session 都跟随角色切换；某个 Character 等待模型回复时仍可切换到其他 Character 继续聊天。

页面支持：

- 左侧 Character 切换、最近消息预览和未读红点；
- `/v1/characters/summaries` 一次返回全部人物摘要，不按人物逐个请求；
- Enter 发送、Shift+Enter 换行；
- `☺` 打开当前人物可用的 Sticker 面板；
- 用户可单独发送一个 Sticker；
- 人物一轮按顺序显示 0~3 条 MESSAGE / EMOJI / STICKER；
- 真正沉默时不伪造角色消息，只显示轻量 `已读 · 没有回复`；
- 非流式等待时显示“正在输入中”；
- 聊天历史与时间分隔；
- 顶部 `Intent` 按钮查看当前人物的 Intent Preview；
- 每条有 Trace 的角色消息可点 `想法` 查看安全 Perception / Reaction 摘要；
- 每条有 Trace 的消息通过 `···` 打开 Developer Detail；
- 查看 Mental State Before / After；
- 查看本轮 Recall；
- 查看实际发送给模型的 messages；
- 查看 Compiled Context / Relationship Time / Available Stickers；
- 查看 Memory Candidate 的 WRITE / SKIP_LOW_VALUE / SKIP_DUPLICATE；
- 查看 Intent Candidate、创建的 Intent ID 和来源 Event；
- 查看 Sticker 决策，包括无效 Sticker ID 被丢弃的原因；
- 查看 Raw Model Response；
- 查看本轮 `runtime_init / recall / model / memory_embedding / persist / API / browser` 等耗时；
- 顶部 `Runtime` 按钮按需查看当前 Character 的 Persona、Mental State、Memory、Intent、Sticker、Provider。

前端不直接操作 Runtime/SQLite，只调用 FastAPI。

## Character / Persona

Character 直接从以下目录自动发现：

```text
personas/<character_id>/persona.yaml
```

不维护第二份 Character 注册表。新增人物只需新增一个 persona 文件。

当前内置：

- `rin`：25 岁，慢热、有自己的节奏；
- `momo`：22 岁，可爱、活泼、有主见的女生；
- `haru`：24 岁，非常温柔、耐心但有稳定判断的男生；
- `rei`：23 岁，表面冷淡、真正感兴趣时会明显热情的女生。

Persona 不只控制语气，也描述：

- 标点、emoji、颜文字和句子节奏；
- 自然追问；
- 沉默；
- 主动；
- 关心；
- 分歧；
- 边界行为。

## Multi-action / Silence / Sticker

新聊天主 contract：

```json
{
  "perception": "可选的一句安全摘要",
  "reaction": "可选的一句安全摘要",
  "mental_state_update": "没有持续变化时可以为空",
  "actions": [
    {"type": "MESSAGE", "message": "诶？？"},
    {"type": "STICKER", "sticker_id": "round_cat_pleading"},
    {"type": "MESSAGE", "message": "怎么回事呀？"}
  ],
  "memory_candidates": [],
  "intent_candidates": []
}
```

一轮最多 3 个 action，但不要求拆分；普通一条消息仍然是默认情况。

`STICKER` 不接受任意 URL。Runtime 每轮会把当前人物可用的 Sticker Manifest 编译进 `# Available Stickers`，模型只能选择真实存在的 `sticker_id`；模型编造不存在的 ID 时 Runtime 会丢弃该动作并记录 Trace。

真正不想回复：

```json
{"actions": []}
```

用户发了消息 ≠ 人物必须回复。明确说“不用回复”、对话自然结束、需要空间或确实没有想说的话时，沉默是合法产品行为。

## Sticker Catalog

人物可在自己的 Persona 目录覆盖默认表情包：

```text
personas/<character_id>/stickers/
├── manifest.yaml
├── happy.webp
├── speechless.gif
└── sleepy.png
```

Manifest 示例：

```yaml
stickers:
  - id: happy
    file: happy.webp
    label: 开心
    tags: [开心, 庆祝]
```

若人物目录没有 `stickers/manifest.yaml`，使用程序内置的原创圆鸭/圆猫测试包。当前支持 PNG / WebP / GIF / SVG / JPG。内置测试包只是验证 Sticker 行为与 UI，不包含从互联网复制的小刘鸭、蜜桃猫等第三方 IP 素材。

用户点击 Sticker 后，Event 内会保留 `sticker_id`，同时把 Sticker 的标签语义转成文本上下文交给当前文本模型，因此人物能够理解用户发来的表情包含义，而不要求默认聊天模型具备视觉能力。

## Intent / Proactive Message

正常聊天的一次 Person Model 调用同时可以返回 `intent_candidates`。只有人物真的形成未来行动意图时才应产生 Intent；没有必要时保持空数组。

到达 `earliest_at` 后，后台 Dispatcher 会把已持久化 Intent 作为 `PROACTIVE_INTENT` 再交给 PersonRuntime 判断是否执行、沉默或放弃。没有 due Intent 时只做轻量 SQLite 检查，不调用模型。

P0.7 起 Intent 记录 `source_event_id`。WebUI 顶部 `Intent` 可查看：

- PENDING / PROCESSING / EXECUTED / SUPPRESSED / DEFERRED / EXPIRED / ERROR；
- Intent 内容；
- created / earliest / expires；
- 产生它的 Event；
- 当时保存的 reason。

这样可以区分“模型根本没产生 Intent”“还没到时间”“到期后选择不发”“已经执行”。

## Memory Admission / Recall

Memory Candidate 不再无条件落库。

当前 admission baseline：

```text
importance < 0.35       -> SKIP_LOW_VALUE
exact duplicate         -> SKIP_DUPLICATE
embedding cosine >= .93 -> SKIP_DUPLICATE
otherwise               -> WRITE
```

这些阈值只是 Eval baseline，后续根据真实数据调整。

Recall 继续使用：

```text
0.70 semantic + 0.20 recency + 0.10 importance
```

并保持 future-memory barrier：只能 Recall `event_time <= now` 的 Memory。

## Relationship Time / Re-encounter

每轮 Runtime 会看到：

```text
# Relationship Time
- 上次聊天时间：...
- 距离上次聊天：7 天
```

人物自己决定是否提旧事、问结果、还是完全不提。

不会写死：

```text
if gap > N:
    say("好久不见")
```

目标是让“昨天说过的事”“隔几天回来”自然影响行为，而不是机械时间模板。

## Developer Inspector / Safe Thought Summary

用户聊天页中的 `想法` 只展示：

- `perception`
- `reaction`

它们是简短、安全、可调试的人物反应摘要，**不是模型隐藏 chain-of-thought**。

完整 Developer Trace 仍用于开发调试，包括 Context、Recall、Actions、Sticker Decision、Memory Admission、Mental State、Intent、Raw Structured Response 和 timings。

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

Trace 不放进 `ACTION.metadata_json`，使用独立 `runtime_traces`。

当前数据库中：

```text
events
memories
mental_states
intents
world_states
runtime_traces
```

正常聊天历史只查询 `USER_MESSAGE / CHARACTER_MESSAGE`；只有点击详情时才读取对应 Trace，避免每次页面刷新解析大量 Context / Prompt / Raw Response。

## 一轮写入一致性

原始用户 Event 会先持久化，因为它是 Source of Truth。

LLM 成功后，以下派生状态在一个 SQLite transaction 中提交：

```text
Mental State
Accepted Memory
Intent
0..3 Character Messages / Stickers
Runtime Trace
ACTION Event
```

如果派生写入失败，则整组 rollback，避免出现“状态改了一半、Trace 又没有”的半轮数据。

## Provider

默认聊天模型是 OpenCode Go 上的 DeepSeek V4.1 Flash：

```text
deepseek-flash
```

OpenCode Go inference 会携带 `x-opencode-session` / `x-opencode-client` / `User-Agent`。

结构化调用使用：

```json
{"response_format":{"type":"json_object"}}
```

JSON 语法由 Provider 约束，业务 contract 继续由 Pydantic validation 负责。

Web 前端为每个 Character 分别把 `conversation_id` 保存在浏览器 `localStorage`；Provider adapter 会将 conversation ID 稳定映射为 UUID。

`httpx.Client` 在 Model 生命周期内复用，不再每次请求重新建立连接。

当前不把 `deepseek-flash` 假设为视觉模型。任意图片输入 / Vision 仍留到后续独立 Media 能力。

## 后端日志

Web/API 默认输出 `INFO` 日志：

```text
API → ChatService → Runtime Event → Recall → Context → Provider → Actions → Memory Admission → Persist
```

单轮日志会输出各阶段耗时。Provider HTTP error body 会直接输出，但不会输出 API Key。

更细日志：

```powershell
$env:CHARACTER_MEMORY_LOG_LEVEL="DEBUG"
uv run character-memory web
```

## Eval / Test

```powershell
uv run pytest -q
uv run character-memory eval evals/smoke.jsonl
uv run character-memory eval evals/p0_relationship.jsonl
```

P0 Relationship suite 当前是：

```text
4 Characters × 6 scenarios = 24 cases
```

覆盖普通聊天、Memory Precision、情绪/追问、观点冲突、明确沉默、重要事件写入、7 天后 Recall / Re-encounter，并按 tag 汇总 pass/fail。

Persona 主观自然度仍需要后续 blind judge；当前 harness 只验证可观测 contract。

## 文档职责

- `docs/DESIGN.md`：产品与 Persistent Person 已确认原则。
- `docs/ARCHITECTURE.md`：当前工程边界与数据流。
- `docs/MEMORY.md`：Memory / Embedding / Admission / Recall 原则。
- `docs/PERSON_RUNTIME.md`：Reaction / Multi-action / Mental State / Silence / Re-encounter / Safe Thought。
- `docs/EVALS.md`：评测计划与 P0 Relationship suite。
- `docs/P0_6_PROACTIVE.md`：主动消息与未读闭环。
- `docs/P0_7_STICKER_INTENT.md`：Sticker、Intent Preview 与 V4.1 Flash 切换。
- `docs/RESEARCH.md`：外部研究参考。

## 当前明确不做

V0 不引入 LangChain/LangGraph、Redis、Celery、PostgreSQL、Knowledge Graph、复杂 Emotion 数值系统、Voice/TTS、Avatar、Video、完整 Feed、多用户生产架构。

当前仍不做任意图片上传 / Vision、搜索引擎自动抓第三方图片、头像自动替换和 External Information / Web Tool Agent。当前人物不知道实时事实时应承认不知道或自然询问；后续如果加入外部能力，再采用 `人物决定查询 -> 外部结果 Event -> Person Runtime 再反应` 的路径。

先把 **同一个人持续聊天、会记、会沉默、会主动回来、会自然使用自己的表情包** 跑稳，再扩展外部能力。
