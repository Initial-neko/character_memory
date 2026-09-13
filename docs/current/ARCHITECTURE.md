# Current Architecture

本文描述当前 `main` 的工程结构与运行边界。源码 HEAD 始终是最终事实源。

## 1. Runtime topology

当前开发栈由三个独立服务组成：

```text
                         ┌──────────────────────┐
Browser Chat UI ────────>│ Character Runtime    │ :8000
                         │ FastAPI               │
                         │ Person Runtime        │
                         │ SQLite / Media        │
                         │ Cloud LLM / Vision    │
                         │ ImageGen providers    │
                         └──────────┬───────────┘
                                    │
Voice browser flow                  │ text/SSE
        │                           │
        v                           v
┌──────────────────────┐     ┌──────────────────────┐
│ Media Runtime        │     │ Dev Console          │ :8002
│ :8001                │     │ diagnostics/proxy    │
│ local ASR + TTS      │     │ no provider keys     │
└──────────────────────┘     └──────────────────────┘
```

推荐开发入口：

```bash
bash scripts/sync-all.sh
uv run character-stack
```

`character-stack` 只负责编排；Character / Media / Dev 仍是三个独立进程。它会复用已经健康的服务，因此修改代码后需要确认旧进程确实已经重启。

## 2. Character Runtime 主路径

WebUI 当前不是同步 RPC 聊天。

```text
User input
  ↓
POST /v1/chat/messages
or POST /v1/groups/{id}/messages
  ↓
先持久化 User Event
  ↓
HTTP 202
  ↓
ReactionScheduler
  ↓ quiet/burst window
PersonRuntime / GroupConversationService
  ↓
SQLite derived state + character events
  ↓
SSE progressive delivery
```

Source Event 先成为 durable fact。Provider 超时或人物 reaction 失败，不允许把用户已经发送的事实一起丢失。

同一 conversation 的新用户事实可以 supersede 尚未提交的旧 generation。旧 generation 的派生状态不会在新事实之后错误落库。

## 3. Application bundle

`AppBundle` 持有：

- `Settings`
- 一个共享 `SQLiteStore`
- `runtimes: dict[character_id, PersonRuntime]`
- 一个共享 Embedding provider
- 一个共享 OpenAI-compatible Person Model
- `ChatService`
- frozen Life / Ticker / DayRunner
- Clock

不同人物共享 Provider、Embedding 与 SQLite，但 Persona、Memory、Mental State、聊天历史和 Runtime 行为按 Character 隔离。

并发边界：

```text
同一 direct character turn       -> per-character lock
同一 group reaction              -> group turn lock + sequential members
不同 character / conversation    -> provider HTTP 可以 overlap
```

群聊成员保持顺序判断，是为了让后一个人物可以看到前一个人物刚刚公开表达的内容；不是为了追求表面吞吐量而并行所有成员。

## 4. Person Runtime

主入口：

```python
result = person_runtime.handle(event)
```

核心信息流：

```text
Event
  ↓
Relationship Time
  ↓
Memory Recall
  ↓
Persona + Mental State + Available Resources
  ↓
Person Model
  ↓
PersonReaction
  ├─ perception / reaction
  ├─ mental_state_update
  ├─ actions[0..3]
  ├─ memory_candidates[]
  └─ intent_candidates[]
  ↓
validation / resource sanitization / admission
  ↓
transactional derived persistence
```

当前 outward primitives：

- `MESSAGE`
- `EMOJI`
- `STICKER`
- `IMAGE`

内部工具意图：

- `GENERATE_IMAGE`

`GENERATE_IMAGE` 本身不直接显示为消息；Direct Visual Runtime 在后台把它执行成真正的 `IMAGE` Event。

`actions=[]` 是合法沉默。模型必须显式给出 `actions`，不能把缺失主行为字段自动猜成 silence。

辅助 `memory_candidates` / `intent_candidates` 的轻微结构漂移会尽量局部归一化或丢弃，避免一个非关键评分字段把已经正确的主回复一起毁掉。

## 5. Direct vs Group facts

### Direct

Direct chat 使用核心 `events` 表。每个 Event 带 `character_id`，conversation id 通过 metadata 保留。

### Group

Group chat 使用独立共享事实表：

```text
conversations
conversation_members
conversation_events
conversation_runtime_traces
```

同一条群消息只存一次。各 Character 可以对这条共享事实形成不同 reaction / memory，但不会把房间事实复制成 N 份彼此无关的用户 Event。

群聊某一个成员 Provider/structured-output 失败时，该成员本轮可以失败，后面的成员继续判断；只有所有成员都失败时才升级为 group-level error。

## 6. SQLite

核心表包括：

- `schema_migrations`
- `events`
- `memories`
- `mental_states`
- `mental_state_history`
- `intents`
- `media_assets`
- `world_states`
- `runtime_traces`
- `conversations`
- `conversation_members`
- `conversation_events`
- `conversation_runtime_traces`

SQLite 是当前单机事实源。连接由进程内 `RLock` 保护，并通过 migration ledger 做幂等 schema 演进。

当前不宣称支持多进程共享同一 ReactionScheduler / SSE hub 的生产级并发。若未来启用多个应用 worker，需要先引入 durable/shared job queue 与 cross-process event transport。

## 7. Memory

正式路径：

```text
Event
  ↓
Memory Candidate
  ↓ deterministic admission
Memory + embedding
  ↓
Vector Recall
  ↓
future Person context
```

Event Log 永远优先于 Memory。Memory 是认知派生层，可以重建、合并、遗忘；不能反向篡改原始经历。

默认 local embedding 是 `BAAI/bge-small-zh-v1.5`。SentenceTransformer wrapper 先尝试本地 cache，cache miss 时才允许 Hub fallback；模型缓存不等于进程重启后无需重新把权重加载进内存。

## 8. LLM / Vision provider

`OpenAICompatibleModel` 使用长期 `httpx.Client` 复用连接。

结构化 PersonReaction 调用：

- `response_format={"type":"json_object"}`
- Pydantic validation
- schema-aware repair retry
- 每次调用返回独立 `ModelCallTrace`

`last_request_*` 等字段只是兼容调试信息，不再用全局锁串行整个 Provider 请求。

纯文本默认使用 `chat_model`。`vision_model` 留空时复用 `chat_model`；只有 Provider 需要单独视觉模型时才配置 override。

## 9. Visual generation

图片生成与 Vision 输入是两个不同方向：

- Vision：用户给人物看一张已经存在的图片。
- ImageGen：角色/用户要求系统生成新的图片。

当前 ImageGen Provider abstraction：

```text
VisualPromptPlanner (plain text)
  ↓
ImageGenerationRequest
  ↓
Agnes / msimg
  ↓
MediaStorage / draft / chat IMAGE
```

Prompt planner 只写最终绘图文本，不输出 JSON。Purpose、比例、Reference、Provider、Persistence 都由程序控制。

角色自主：

- `SELFIE`：支持 reference 的 Provider 默认使用当前头像保持身份一致。
- `SCENE`：不强制人物出镜，也不强制头像 reference。
- 当前只允许 direct `USER_MESSAGE` 触发自主生成；Wake/Proactive 不自动花费 ImageGen 配额。

显式用户工具则可以在 Direct/Group Chat 中生成图像草稿，再由用户手动发送。

## 10. Media Runtime

`:8001` 独立拥有本地 ASR/TTS 模型：

- SenseVoice / sherpa-onnx ASR
- VITS / sherpa-onnx TTS

它不 import / instantiate `PersonRuntime`。Main LLM、Vision、Memory 都留在 Character Runtime。

Windows 上 native ONNX Runtime 必须来自项目 `.venv` / sherpa wheel，不允许静默退回 `C:\Windows\System32\onnxruntime.dll`。

## 11. Dev Console

`:8002/dev` 是当前开发前门，用于：

- Character / Media health
- LLM probe
- ASR / TTS
- Media live smoke
- ImageGen provider / rewrite / generate / preview
- system RAM / process RSS / NVIDIA VRAM
- recent Media metrics

Dev Console 不持有云 API key，不是任意 URL/header 的 Postman 替代品。

## 12. Web UI

正式聊天仍使用原生 HTML/CSS/JS，无 React 构建链。

主要前端模块按职责拆分：

- `app.js` — conversation state / direct send / renderer
- `groups.js` — group UX
- `images.js` — pasted/file image draft
- `ai_images.js` — explicit AI generated image draft
- `stickers.js` — global sticker catalog
- `avatars.js` — avatar manager
- `search.js` — message search
- `mentions.js` — group mentions
- `voice.js` / `dictation.js` — voice input/call flow
- `realtime_reconcile.js` — SSE/reconciliation helper

历史 `p0_*.css` 仍存在于前端资源中，属于视觉样式技术债；不要再增加新的 milestone 命名 CSS。后续若整理样式，应按 feature/layout 职责合并，而不是继续按 P0 编号叠加。

## 13. Deliberate boundaries

当前没有因为功能增长而引入：

- LangChain / LangGraph
- Redis / Celery
- PostgreSQL / pgvector
- Knowledge Graph
- React / Next.js
- 多 worker durable reaction queue

这些不是永远禁止，而是必须由真实瓶颈或产品 contract 证明必要。
