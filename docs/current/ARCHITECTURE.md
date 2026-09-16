# Current Architecture

本文描述当前 `main` 的工程结构与运行边界。源码 HEAD 始终是最终事实源。

## 1. Runtime topology

当前 `character-stack` 编排五个独立进程：

```text
Browser
├─ Chat UI ------------------------------------------------------┐
├─ Dev Console :8002                                            │
├─ Settings Center :8003                                        │
└─ TTS Provider Lab :9002                                       │
                                                                │
Character Runtime :8000                                         │
├─ FastAPI / Direct / Group / SSE                               │
├─ ReactionScheduler / PersonRuntime                            │
├─ Persona / Memory / Mental State / Intent                     │
├─ Vision + Visual Capture context                              │
├─ ImageGen / autonomous visual                                 │
└─ SQLite + local media metadata/files                          │
                                                                │
Media Runtime :8001 <-------------------------------------------┘
├─ SenseVoice ASR
├─ Sherpa VITS fallback
└─ formal /v1/tts router
      └─ Kokoro -> TTS Provider Runtime :9002/v1/tts

Optional CosyVoice sidecar :9012
```

推荐开发入口：

```bash
bash scripts/setup-media-models.sh
uv run character-stack
```

仅重新同步 Python 开发依赖时：

```bash
bash scripts/sync-all.sh
```

`character-stack` 只负责编排；各服务仍是独立进程。它会复用已经健康运行的服务，因此修改代码后需要确认旧进程确实已经重启。

`:9002` 在 V1 同时承担两个角色：

```text
TTS Provider Runtime
+
TTS Lab UI
```

这是当前实现事实，不代表长期架构必须保持耦合。后续如果 Provider Runtime 与试听 UI 的职责开始互相干扰，再在大版本中拆分。

## 2. Character Runtime 主路径

WebUI 当前不是同步 RPC 聊天。

```text
User input
  ↓
POST /v1/chat/messages
or POST /v1/groups/{id}/messages
or Visual Capture message route
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

`AppBundle` 持有或装配：

- `Settings`
- 一个共享 `SQLiteStore`
- `runtimes: dict[character_id, PersonRuntime]`
- 一个共享 Embedding provider
- 一个共享 OpenAI-compatible Person Model
- `ChatService`
- Reaction/SSE wiring
- Visual runtime
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

`GENERATE_IMAGE` 本身不直接显示为消息；Direct 与 Group 都复用现有 VisualPromptPlanner / Provider / MediaStorage，在主 reaction 提交之后异步追加真正的 `IMAGE` Event。

`actions=[]` 是合法沉默。模型必须显式给出 `actions`，不能把缺失主行为字段自动猜成 silence。

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

Group 也支持 soft archive/restore。Archive 隐藏 conversation，但不删除 `conversation_events`、Trace、Memory 或 Media。

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

默认 local embedding 是 `BAAI/bge-small-zh-v1.5`。

## 8. LLM / Vision provider

`OpenAICompatibleModel` 使用长期 `httpx.Client` 复用连接。

结构化 PersonReaction 调用使用 JSON object contract + Pydantic validation + repair retry。

纯文本默认使用 `chat_model`。`vision_model` 留空时复用 `chat_model`；只有 Provider 需要单独视觉模型时才配置 override。

## 9. Visual input: file vs live capture

视觉输入有两类，但都进入同一个 PersonRuntime：

```text
User image attachment
  -> durable MediaAsset reference
  -> current-turn Vision

Camera / Screen Share
  -> browser keyframe sampling
  -> transient image_data_urls
  -> current-turn Vision
```

Visual Capture 当前支持 `CAMERA` 与 `DISPLAY`：

- 单次请求最多 5 帧；
- 单帧最多 2 MiB；
- 总计最多 6 MiB；
- JPEG / PNG / WebP；
- Direct / Group 都支持。

关键约束：**Capture frame bytes 不是聊天附件。** Event 只保存 `frame_count / sources / captured_at_ms` 等摘要 metadata，真正帧只用于当前模型 turn。

详见 [`VISUAL_CAPTURE.md`](VISUAL_CAPTURE.md)。

## 10. Visual generation

ImageGen 与 Vision/Capture 是相反方向：

```text
看：existing image / camera / screen -> Vision
画：Character/User intent -> ImageGen provider -> new image
```

当前 Provider abstraction：

```text
VisualPromptPlanner (plain text)
  ↓
ImageGenerationRequest
  ↓
Agnes / msimg
  ↓
MediaStorage / draft / chat IMAGE
```

角色自主：

- Direct `USER_MESSAGE` reaction 可以产生 `GENERATE_IMAGE`；
- Group 中每个 Character 的 `USER_MESSAGE` reaction 也可以独立产生 `GENERATE_IMAGE`；
- `SELFIE` 在 Provider 支持 reference 时默认用当前 avatar；
- `SCENE` 不强制人物出镜或 avatar reference；
- Wake/Proactive 不自动生成图片；
- 每个 Character 单轮最多一个自主生成任务。

Group 不建立第二套 ImageGen。生成完成后以对应 Character 身份写入 `conversation_events`，并通过现有 group SSE 推送。

显式用户生图工具仍先生成 draft，再由用户确认发送。

详见 [`VISUAL_GENERATION.md`](VISUAL_GENERATION.md)。

## 11. Media Runtime and formal TTS

`:8001` 独立拥有本地媒体能力：

- SenseVoice / sherpa-onnx ASR；
- Sherpa VITS fallback；
- 正式 Browser TTS 稳定入口 `/v1/tts`。

正式 TTS route 根据 `config.yaml` 的：

```yaml
tts_provider: kokoro
tts_voice: zf_001
tts_speed: 1.0
tts_device: cpu
```

选择实现。

当 `tts_provider: kokoro`：

```text
Browser -> :8001/v1/tts -> :9002/v1/tts -> Kokoro
```

当 `tts_provider: sherpa` 时，`:8001` 直接使用本地 Sherpa runtime。

Media Runtime 不 import / instantiate `PersonRuntime`。Main LLM、Vision、Memory 都留在 Character Runtime。

Windows 上 native ONNX Runtime 必须来自项目 `.venv` / sherpa wheel，不允许静默退回 `C:\Windows\System32\onnxruntime.dll`。

## 12. Settings Center

`:8003/settings` 是本地配置管理入口。

持久化规则：

```text
config.yaml   non-sensitive runtime config
.env          API keys / tokens
```

Secret precedence：

```text
system environment > .env > legacy config.yaml secret
```

Settings Center 会迁移已知 legacy plaintext Secret，普通 config save 会在替换前创建 timestamped `.bak`。浏览器不会拿到现有 Secret 明文。

V1 明确采用 restart policy：修改配置后重启 stack，让所有服务读取同一份 coherent snapshot。

## 13. TTS Provider Runtime + Lab

`:9002` 当前暴露：

- Kokoro 82M v1.1 zh；
- Sherpa（通过 `:8001`）；
- optional CosyVoice sidecar `:9012`。

Lab 下拉选择只用于试听/benchmark，不会自动改变正式 TTS 默认值。正式 provider/voice 由 Settings Center / `config.yaml` 决定。

当前 Kokoro V1 默认 voice 为 `zf_001`，可选 `zf_001..zf_004`。模型与 voice 由 `scripts/setup-media-models.sh` 预下载，正常 request path 不应临时联网下载模型文件。

## 14. Dev Console

`:8002/dev` 用于：

- Character / Media health；
- LLM probe；
- ASR / formal TTS；
- Media live smoke；
- ImageGen provider / rewrite / generate / preview；
- system RAM / process RSS / NVIDIA VRAM；
- recent Media metrics。

Dev Console 不持有云 API key，不是任意 URL/header 的 Postman 替代品。Secret 编辑归 Settings Center。

## 15. Web UI

正式聊天使用原生 HTML/CSS/JS，无 React 构建链。

主要前端模块：

- `app.js` — conversation state / direct send / renderer
- `groups.js` — group UX
- `images.js` — pasted/file image draft/send
- `ai_images.js` — explicit generated-image source
- `visual_capture.js` — camera/display sampling + keyframe selection
- `visual_client.js` — visual request helper
- `stickers.js` — sticker catalog/import UI
- `avatars.js` — avatar manager
- `search.js` — message search
- `mentions.js` — group mentions
- `voice.js` / `dictation.js` — voice input/call flow
- `realtime_reconcile.js` — SSE/reconciliation helper
- `time_format.js` — shared `MM-DD HH:mm:ss` chat timestamp formatter

历史 `p0_*.css` 仍是正式加载资源，属于样式技术债。V1 不为了目录美观做大规模重命名；下个大版本再按 feature/layout 职责整理。

## 16. Deliberate boundaries

当前没有因为功能增长而引入：

- LangChain / LangGraph
- Redis / Celery
- PostgreSQL / pgvector
- Knowledge Graph
- React / Next.js
- 多 worker durable reaction queue

Life Simulation 与 Streamlit Inspector 仍有正式入口，因此 V1 保留但冻结扩张。是否整体移除属于后续大版本决策，不做零碎删除。

这些边界不是永远禁止，而是必须由真实瓶颈或产品 contract 证明必要。
