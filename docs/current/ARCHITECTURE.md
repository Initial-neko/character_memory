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
├─ SpaceAutonomyScheduler / GroupAutonomyScheduler              │
├─ Persona / Memory / Mental State / Intent                     │
├─ Vision + Visual Capture context                              │
├─ ImageGen / autonomous visual                                 │
└─ SQLite + local media metadata/files                          │
                                                                │
Media Runtime :8001 <-------------------------------------------┘
├─ SenseVoice ASR
├─ Sherpa VITS local runtime
├─ Workbench-only direct Sherpa route /v1/providers/sherpa/tts
└─ formal /v1/tts router
      ├─ Sherpa -> local VITS
      └─ Kokoro / Edge / GSV -> :9002/v1/tts
                                  └─ GSV -> :9014

Optional CosyVoice sidecar :9012
Manual Qwen3-TTS 0.6B experiment :9013
GSV-TTS-Lite sidecar :9014 when isolated runtime exists
Optional Qwen3 VoiceDesign tool :9015 (not a formal chat Provider)
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

`character-stack` 只负责编排；各服务仍是独立进程。它会复用已经健康运行的服务，因此修改代码后需要确认旧进程确实已经重启。运行中的 launcher 把 pid 写在 `.debug-output/stack.pid`，`--stop` 通过 `.debug-output/stack.stop` 请它自己按启动的逆序收掉子进程；`--status` 只报告状态。两者都作用于当前 checkout，不依赖外部的进程树探测。

`:9002` 在 V1 同时承担 Provider Runtime 与 TTS Workbench。Workbench 还承载 optional VoiceDesign UI，但 Qwen3 VoiceDesign 本身不是正式聊天 Provider。正式 Provider id/默认 voice/device lifecycle metadata 集中在 `tts_registry.py`，避免 Config / Settings / Media / Workbench 各自维护一份名单。

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

## 3. Application bundle and feature-service composition

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

HTTP feature route 不再负责创建 Search / Avatar / ImageGen / World infrastructure。Character Runtime 在 `create_api()` 阶段通过 `RuntimeServices` 一次装配，并通过 typed `CharacterRuntimeAccess` 暴露稳定核心依赖；不再使用无契约的 `SimpleNamespace` 作为核心 service locator：

```text
RuntimeServices
├─ shared SearchProvider
├─ AvatarStore + AvatarSearchService
├─ ImageGenerationProviders
├─ HeadlessBrowserWebFetcher
└─ WorldObservationService
```

Avatar、Space Image Search 与 World Observation 可以共享同一个 SearchProvider，但 ownership 不属于 Avatar route；Visual/Space 共用 ImageGen provider，也不依赖 `attach_visual_routes()` 是否先执行。Route module 只暴露 HTTP adapter，不能再用 attach 顺序充当依赖注入机制。

核心 HTTP 也采用同样原则。历史上接近千行的 `api.py` 不再同时承担 Character/Sticker/Image/Direct route 实现；现在：

```text
create_api()
├─ lazy AppBundle / RuntimeServices / lifecycle
├─ ApiCharacterService
├─ ApiResourceService
├─ CharacterRuntimeAccess      -> feature modules
└─ CoreApiRouteAccess          -> core route modules
    ├─ core_character_web.py
    ├─ core_resource_web.py
    └─ core_direct_web.py
```

因此并行开发 Character、资源管理、Direct diagnostics 时不需要反复修改同一个 composition-root 文件。新增 endpoint 应进入所属 route module；只有新增跨 feature 依赖或生命周期 ownership 时才修改 `api.py`。

并发边界：

```text
同一 direct character turn       -> per-character lock
同一 group reaction              -> group turn lock + sequential members
不同 character / conversation    -> provider HTTP 可以 overlap
```

群聊成员保持顺序判断，是为了让后一个人物可以看到前一个人物刚刚公开表达的内容；不是为了追求表面吞吐量而并行所有成员。

## 4. Person Runtime

Direct 主入口仍是：

```python
result = person_runtime.handle(event)
```

Direct 与 Group 不再各自维护一整套 reaction pipeline。当前边界是：

```text
User text/sticker/image
  ↓
IncomingMessageNormalizer
  ↓
Direct Event / Group Event
  ↓
ReactionEngine
  ├─ PersonContextBuilder
  │   ├─ Persona
  │   ├─ Mental State
  │   ├─ Memory Recall
  │   └─ Recent Events
  ├─ base + channel context
  ├─ Person Model
  └─ resource sanitization
  ↓
PersonReaction
  ├─ perception / reaction
  ├─ mental_state_update
  ├─ actions[0..3]
  ├─ memory_candidates[]
  └─ intent_candidates[]
  ↓
channel policy
  ↓
shared Action materialization
  ↓
Direct / Group persistence + derived-state commit
  ↓
canonical Message projection
```

因此“人物如何读取状态、调用模型、校验资源、把 outward action 变成消息”的主要机制是共享的；“事实存哪张表、Group 多成员顺序/@mention/privacy、Direct Intent/Wake/Proactive”仍是显式 channel policy。

Space/World planning 继续共享 `PersonContextBuilder`，但还没有完全进入 `ReactionEngine`；这个差异是已登记的后续 cognition convergence，而不是用 Group/Direct schema 强行套住 Space。

对外表达与内部工具 Action 的完整 contract 由 [PERSON_RUNTIME.md](PERSON_RUNTIME.md) §2 维护；枚举和 materialization 边界以 [domain/models.py](../../src/character_memory/domain/models.py) 为准。此处只描述执行关系，不维护第二份能力清单。

`GENERATE_IMAGE` 本身不直接显示为消息；Direct 与 Group 都复用现有 VisualPromptPlanner / Provider / MediaStorage，在主 reaction 提交之后异步追加真正的 `IMAGE` Event。

`actions=[]` 是合法沉默。模型必须显式给出 `actions`，不能把缺失主行为字段自动猜成 silence。

## 5. Durable facts: Direct / Group / Space

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


### Space

Character Space 使用另一组 shared durable facts：

```text
space_posts
space_comments
space_reactions
space_views
space_post_media
space_opportunity_state
space_opportunity_runs
```

因此当前更准确的原则是 **Durable Facts > Derived Cognition**，而不是“所有事实都必须写进单一 `events` 表”。

```text
Direct fact        -> events
Group shared fact  -> conversation_events
Group autonomy     -> hidden GROUP_OPPORTUNITY provenance + CHARACTER_MESSAGE
Space shared fact  -> space_*
Media fact         -> media_assets
                     ↓
Derived cognition  -> Memory / Mental State / Intent / Trace
```

不同事实表可以有不同共享语义，但 Memory/State 仍属于同一个 Persistent Person，且不能反向篡改原始事实。

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

SQLite 是当前单机事实源。连接由进程内 `RLock` 保护，并通过 migration ledger 做幂等 schema 演进。持久化数据库连接在 schema 初始化前启用 SQLite **WAL** 模式，允许不同连接的读快照与写提交并行；跨连接写者仍然串行，普通核心连接最多等待 15 秒取得 SQLite 锁。只读/调用统计连接继续使用其自身独立的短等待策略，不能通过提高业务写等待把遥测写入变成聊天延迟。此策略缓解正常读写竞争，不承诺外部进程持有长期写事务时不会超时；也不意味着能够启动多个相互独立的 Character Runtime workers。

Core Web 在 Runtime 懒初始化前后复用同一个 `SQLiteStore`：Web 装配拥有连接，后创建的 AppBundle 借用它。World 回执、观察和个人认知的本地事务不能通过另一连接更新自己持有写锁的数据库。独立 CLI AppBundle 仍拥有自己的 Store。事务主体或提交失败均回滚并恢复事务深度。

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

Durable source fact 永远优先于 Memory。Memory 是认知派生层，可以重建、合并、遗忘；不能反向篡改 Direct / Group / Space 等原始经历。

默认 local embedding 是 `BAAI/bge-small-zh-v1.5`。Character Web 进程启动后立即在后台 warm Person Runtime/Embedding，同时保持 `/health` 可用。SentenceTransformer runtime 使用 strict `local_files_only=True`；模型下载只允许出现在 setup/prefetch 阶段，不允许正常启动或第一句话临时访问 Hub。

Memory recall 不再为每一轮把整个历史 Memory 全部解包并做 NumPy cosine。SQLite 先提供有上限的候选集（最近 768 + 最高 importance 256 的去重并集），随后执行现有 semantic/recency/importance 排序；exact duplicate admission 仍可跨全部 active Memory 精确查找。这个边界延缓长期 Memory 的 O(N) 增长，同时不提前引入向量数据库。

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

详见 [`VISUAL.md`](VISUAL.md)。

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

详见 [`VISUAL.md`](VISUAL.md)。

## 11. Character Space and World Observation

Character Space 是同一个 Persistent Person 的公共表达渠道，不是第二套 Persona。

```text
Space Opportunity
  -> optional WorldExplorePlan
  -> SearchProvider.search_web
  -> Headless Chromium rendered page
  -> WorldObservationAppraisal
  -> optional safe cognition/memory path
  -> SpacePostPlan
  -> optional Search Image / ImageGen
  -> shared Space facts
  -> sparse Audience reactions through PersonRuntime
```

World Search、Browser Render、Memory、Public Expression 是四个不同边界。搜索到页面不等于相信、记住或公开表达；原始网页文本是 untrusted data，不直接写进 Memory 或最终发帖 prompt。

Space/World planning 已通过 `PersonContextBuilder` 读取同一人物的身份、状态、记忆和经历。不同渠道仍保留专用 decision/appraisal Schema 与 Prompt；这是当前明确的边界，不是第二套人格状态。是否进一步收敛决策层，应由跨渠道、长期人物验收决定，不提前引入巨大统一 Schema。

详见 [`SOCIAL_WORLD.md`](SOCIAL_WORLD.md) 与 [`MEMORY.md`](MEMORY.md)。

## 12. Media Runtime and formal TTS

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

正式 Browser 始终调用 `:8001/v1/tts`：

```text
sherpa -> local VITS
kokoro -> :9002 -> Kokoro
edge   -> :9002 -> Edge online TTS
gsv    -> :9002 -> :9014 GSV-TTS-Lite
```

Workbench 试听 Sherpa 使用独立 `:8001/v1/providers/sherpa/tts`，因此不会经过正式 Provider selector。

Media Runtime 不 import / instantiate `PersonRuntime`。Main LLM、Vision、Memory 都留在 Character Runtime。

Windows 上 native ONNX Runtime 必须来自项目 `.venv` / sherpa wheel，不允许静默退回 `C:\Windows\System32\onnxruntime.dll`。

## 13. Settings Center

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

配置不再采用“一律重启整个 stack”的策略。TTS Provider/Voice/Speed 由 Media Runtime 每次请求读取；GSV runtime 配置/device 通过 sidecar 热应用。Kokoro/Sherpa 的 device 属于模型进程初始化参数，变更时只重启对应 `:9002` / `:8001`。其他普通 LLM/storage 配置仍由 Settings 返回明确的 `restart_required`。

## 14. TTS Provider Runtime + Lab

`:9002` 同时承担正式 Provider Runtime 和 Workbench 试听/工具编排。完整 Provider 与 VoiceDesign 能力归 [VOICE_AND_TTS.md](VOICE_AND_TTS.md)；正式 Provider 元数据来自 [tts_registry.py](../../src/character_memory/tts_registry.py)，Workbench adapter 装配来自 [tts_lab.py](../../src/character_memory/tts_lab.py) 的 `TtsLabRuntime`。不在架构概览重复维护试听 Provider 名单。

Lab 下拉选择只用于试听/benchmark，不会自动改变正式 TTS 默认值。正式 provider/voice 由 Settings Center / `config.yaml` 决定。

当前 Kokoro V1 默认 voice 为 `zf_001`，可选 `zf_001..zf_004`。模型与 voice 由 `scripts/setup-media-models.sh` 预下载，正常 request path 不应临时联网下载模型文件。

## 15. Dev Console

`:8002/dev` 用于：

- Character / Media health；
- LLM probe；
- ASR / formal TTS；
- Media live smoke；
- ImageGen provider / rewrite / generate / preview；
- system RAM / process RSS / NVIDIA VRAM；
- recent Media metrics。

Dev Console 不持有云 API key，不是任意 URL/header 的 Postman 替代品。Secret 编辑归 Settings Center。

LLM 用量 GET 使用 SQLite `mode=ro`，不建立或迁移正式库表；无数据库/无用量表时返回内存空视图。实际模型记账仍由写入 Recorder 拥有。示例配置使用独立 example 库；验收必须显式使用临时配置、临时数据库和隔离服务地址，不应启动第二个 Runtime 指向正式库。

## 16. Web UI

正式聊天使用原生 HTML/CSS/JS，无 React 构建链。

主要前端模块：

- `app.js` — conversation state / direct send / renderer
- `groups.js` — group UX
- `images.js` — pasted/file image draft/send
- `ai_images.js` — explicit generated-image source
- `visual_capture.js` — camera/display sampling + keyframe selection
- `visual_client.js` — visual request helper
- `stickers.js` — scoped sticker catalog/import UI (global + current character)
- `avatars.js` — avatar manager
- `search.js` — message search
- `mentions.js` — group mentions
- `voice.js` / `dictation.js` — voice input/call flow
- `realtime_reconcile.js` — SSE/reconciliation helper
- `time_format.js` — shared `MM-DD HH:mm:ss` chat timestamp formatter

历史 `p0_*.css` 仍是正式加载资源，属于样式技术债。V1 不为了目录美观做大规模重命名；下个大版本再按 feature/layout 职责整理。

### 表情包作用域（公共池 + 角色私有池）

- 旧全局表情库（包括内置、全局 ZIP 导入及历史 persona 表情）仍是公共池，旧消息和无 `character_id` 的 `/v1/stickers` 保持原有访问语义。
- 新私有包放在 `sticker_dir/characters/<character-id-hash>/manifest.yaml`，私有 sticker/pack ID 以角色 ID 哈希命名空间隔离。相同图片重复导入复用 ID，图片内容变化时生成新 ID，保留旧消息的资源引用。
- 角色 runtime 的候选池是 `公共池 ∪ 该角色私有池`。群聊每个 AI 成员也按自己的角色读取；群聊用户发送表情仍从公共池选择。
- 导入接口 `POST /v1/stickers/import?scope=character&character_id=<id>` 写私有池，未指定 `scope` 时默认写公共池。前端私聊选择当前角色私有池；群聊导入公共池。
- 全局图片 URL `/v1/stickers/<sticker_id>/asset` 不暴露私有资源；私有图片走 `/v1/stickers/<character_id>/<sticker_id>/asset`，由服务端校验角色归属。
- 当前版本不自动清理旧资产，也不提供删除已使用私有表情的破坏性操作，以免历史聊天出现资源缺失。归属变更需要后续单独做复制/归档事务，不能直接迁移文件。
- `sticker_sheet.py` 支持检测接近三等分的透明分隔线（含非 3 整除尺寸和微弱 Alpha 噪声），仍拒绝不明确的九宫格，不自动猜测跨格图案。

## 17. Deliberate boundaries

当前没有因为功能增长而引入：

- LangChain / LangGraph
- Redis / Celery
- PostgreSQL / pgvector
- Knowledge Graph
- React / Next.js
- 多 worker durable reaction queue

Life Simulation 与 Streamlit Inspector 仍有正式入口，因此 V1 保留但冻结扩张。是否整体移除属于后续大版本决策，不做零碎删除。

这些边界不是永远禁止，而是必须由真实瓶颈或产品 contract 证明必要。
## Character execution evolution boundary

The evolution target is Character + Shared Execution Runtime inside the existing
Python process. This is an implementation boundary, not a second Character Agent:
PersonRuntime remains the owner of reaction and derived cognition; existing
channel-specific decision schemas and schedulers retain their business meaning.
Any new execution contract must have a real capability consumer before it ships.
It may validate permission, arguments, budget and execution facts, but must not
purchase a second model judgment of the Character's willingness.

The baseline is exercised by `tests/test_character_architecture_baseline.py`:
real PersonRuntime, PersonContextBuilder, WorldObservationService and SQLite,
with local model/search/fetch providers and an owning-thread network rejection
guard. The synchronous baseline starts no workers; unrelated callbacks from
other test suites are not attributed to its opportunity. Mock
logical calls are distinguished from HTTP requests and query embeddings.
This proves the deterministic baseline, not live Persona quality or provider
cost. Historical observation recall and finite Intent deferral have independent
engineering gates; optional RSS reading and final live quality require their own
acceptance before rollout.


### Internal World capability execution contract

`runtime/capability_execution.py` separates an already chosen request from its
bounded execution. The composition caller supplies trusted character identity,
opportunity identity, source event and constraints; the model supplies only the
browse choice/query or IDs from the server-provided RSS candidate set. Silence
produces no request. This contract adds
no second Character state, decision model, scheduler or retry loop.

The current adapter accepts World WEB_SEARCH and delegates to the existing
WorldObservationService Search/Browser path. Direct, Group and unknown
capabilities are denied. Its additive `world/004-capability-executions` ledger
reserves one attempt per opportunity before external work. Failure and timeout
consume that allowance; an identical completed request returns its durable
result. Reusing an ID with different inputs is denied. STARTED with no local
receipt is reported as in-progress/interrupted and never automatically replayed.
Provider-specific timeout and HTTP retry behavior remains with existing services;
the one-attempt bound counts capability dispatches, not individual HTTP requests.

Personal Browse is the production consumer and preserves its existing decision
and appraisal calls. This boundary does not change Space,
Media, Direct or Group execution permissions.

The World executor also accepts bounded `READ_RSS` requests from Personal Browse.
Its adapter reads already-stored Feed text through `RssPersonalReading`, which
owns character-local item claims/snapshots; RSS ingestion continues to own source
and shared article data. The same durable execution allowance and result cache
apply. Channel identity and item generations come from server context, not
external text. This is not a general Tool registry or a second Character runtime.

World WEB_SEARCH can opt into `world_browse_execution_backend: claude`; the
default remains `direct`. RuntimeServices owns one bounded ClaudePrintRunner.
Each task starts a fresh local `claude -p` process with no resume/continue/fork
and no session persistence. The fixed execution contract and current public
query are its only application input; Person context stays in the existing
planning/appraisal calls. `--bare`, fresh working/configuration directories and
an explicit MCP configuration prevent inheriting ordinary local customizations.
The runner requires CLI 2.1.286 or newer and an explicit API key or bearer token.
It reads only whitelisted authentication/routing/model fields from the existing
user settings and environment; credentials stay in the subprocess environment.
It does not load subscription OAuth files or the system keychain. The effective
tool list must contain only `mcp__character_world__world_lookup`, with that server
connected. Missing or unexpected tools fail readiness rather than changing mode.
Managed policy requiring additional customization is not silently accepted.

The single `world_lookup` stdio tool delegates through a short-lived authenticated
loopback callback to the existing WorldObservationService. It has no database,
shell, file, posting or Character-state access. Tool calls and page attempts are
bounded across the whole task; callbacks revoke admission on cancellation, while
already-running browser/DNS work retains the existing fetch lifecycle limitation.
The real `world_lookup` callback records bounded tool steps in the existing detail
file and capability result receipt: owning capability request ID, a local `step_id`
(not a Claude JSON-RPC ID), tool name, validated query, UTC start/completion,
elapsed milliseconds, status and source-backed result excerpts. At most
`min(5, max_tool_calls + 1)` callbacks are recorded; additional callbacks set an
explicit truncation flag. Invalid payloads omit their inputs; failures contain
fixed error codes rather than raw exceptions. A result excerpt is at most 1200
characters, with available length and excerpt-truncation recorded separately;
titles and step source URLs are capped at 180/500 characters with explicit
truncation flags (the UI does not open truncated URLs).
This does not certify that the original webpage was fully read. No callback
means an empty recorded list, not tool success. Timeout snapshots may retain
`RUNNING` with no completion/result; inspection shows that outcome as unknown.
Existing credential redaction is applied before persisting steps. Commands,
environment values and hidden model thinking are not added to step records.

Only tool receipts supply readable observations; Claude prose is not proof of a
visit. Failed or interrupted tasks do not enter appraisal.

The additive `world/005-execution-metadata` migration extends the existing
execution ledger, not a second execution system. It records backend, local-day
quota reservation and CLI usage completeness. Unknown spend retains its
reservation; failure consumes the attempt. The optional per-character estimated
dollar budget is enforced only with cost saving enabled and an explicit per-run
reservation. CLI estimates are not authoritative bills or subscription balance.
Existing World planning, one successful-read appraisal and local recovery remain
the cognition owners. Returning to direct stops new Claude dispatches without
deleting execution history or restoring a database backup.

Windows tasks start suspended and enter an owned kill-on-close Job Object before
resuming; timeout/shutdown terminates descendants even if the parent has exited.
Shutdown and launch admission share a lock. POSIX tasks use a process group.
Per-task JSON details keep the public task, visible response, tool receipts and
redacted diagnostics, never hidden reasoning. Details expire after 30 days and
are cleaned on subsequent dispatch; the durable execution ledger remains.
Bounded verified excerpts remain in the existing durable result cache, as for
the direct backend, so interrupted World appraisal can recover without another
external call. They are not duplicated in the transient detail file; the 30-day
policy applies to assistant replies/diagnostics, not to durable reading facts.
Recovery reconstructs the original request including its stored execution policy;
current Settings do not rewrite that policy. Receipt integrity checks remain hard
checks. The owning phase/recovery contract is [Social World](SOCIAL_WORLD.md).

The read-only `/v1/characters/{id}/executions` list and request detail route use
the existing ledger, enforce character ownership and never initialize providers.
Optional local `day` filtering supplies per-character daily usage summaries.
Known subtotals are separate from totals: incomplete costs/token counts keep
the total unknown rather than pretending missing records cost zero.
The Life event detail links its existing capability request to task, response,
actual source scope, connected tools and usage. A response is explicitly not
webpage text or Character cognition. Missing details and incomplete usage remain
distinguishable from a successful empty response or a zero bill. CLI costs are
estimates, not account balance or remaining subscription quota. Inspecting a
record never re-executes it or resumes a Claude session.

V1 exposes this executor only through the existing World WEB_SEARCH choice.
There is no general “ask Claude” decision, automatic Direct question delegation,
native interactive window manager or complete intermediate tool transcript.
The backend defaults to direct; code availability does not establish that a
running deployment has restarted, enabled Claude or passed live Persona quality
acceptance. Character life/source display semantics belong to [Memory](MEMORY.md).
