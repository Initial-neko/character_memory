# Conversation Runtime

本文汇总当前 Direct / Group Chat 的事实存储、异步反应、SSE、Visual Capture、搜索和 Mention contract。历史 P0.x 文档已归档，不再作为当前入口。

## 1. User message first, reaction later

正式 WebUI 发送用户消息时使用异步 accept path：

```text
POST /v1/chat/messages
POST /v1/groups/{conversation_id}/messages
```

带 Camera/Screen 关键帧时使用：

```text
POST /v1/visual/direct/messages
POST /v1/visual/groups/{conversation_id}/messages
```

服务端先：

1. 校验文本/Sticker/Image/Visual Capture；
2. 保存需要长期持久化的媒体；
3. 持久化用户 Event；
4. enqueue reaction；
5. HTTP 202 返回。

用户已经发送的事实不等待 LLM 完成。

Character reaction 由 `ReactionScheduler` 异步处理，通过 SSE 推给前端。

Visual Capture 是例外中的“transient payload”：Camera/Screen frame bytes 不作为普通聊天附件持久化，只把必要 metadata 写进 Event，并把 image data URLs 传给本轮模型 Vision context。

## 2. Burst window

Scheduler 使用短窗口把连续快速输入看成一组先后事实，而不是机械“一句话一定对应一次回复”。

当前 baseline：

```text
quiet window       ≈ 0.5 s
maximum burst      ≈ 1.5 s
```

多个快速用户消息可以先全部持久化，然后针对最新 watermark 产生一次 reaction。

## 3. Watermark / supersession

每次 generation 有 source Event ID watermark。

当模型仍在生成时又来了更新的用户 Event：

```text
old generation
  ↓ commit guard sees newer user fact
SUPERSEDED
  ↓
旧 reaction 不提交 derived state
  ↓
Scheduler 针对最新事实重试
```

Superseded cycle 不应：

- 写旧 Mental State；
- 写旧 Memory/Intent；
- 写过时 Character Message；
- 消耗掉尚未处理的 image/mention/visual signal。

自主 ImageGen 也有 stale guard。Direct 使用现有 `still_current` 语义；Group 使用最新 user event watermark。图片生成期间房间已经进入更新用户事实时，旧图片不会硬插回新 turn。

## 4. SSE

统一入口：

```text
GET /v1/events/stream
```

scope：

- `direct`：需要 `character_id + conversation_id`
- `group`：需要 `conversation_id`

常见事件：

```text
reaction_status
character_event
group_character_event
group_member_complete
reaction_complete
reaction_error
```

`reaction_status` 状态：

```text
queued
typing
superseded
idle
```

### Fresh stream reconciliation

SSE hub 是 ephemeral transport，不是 durable source of truth。

新的 UI stream 建立时：

1. durable chat history 先由 history API 对齐；
2. fresh stream 不重放很久以前的 typing/member events；
3. 服务端额外发送当前 Scheduler 的 authoritative `reaction_status` snapshot。

真正的 EventSource 网络自动重连继续使用 `Last-Event-ID` 尽量补 transient gap。

## 5. Direct chat

Direct Event 存在 core `events` 表中，以 `character_id` 区分人物，并通过 metadata 保留 `conversation_id`。

同一人物 turn 通过 ChatService lock 串行，避免同一个 Character 的 derived state 交叉提交。

不同 Character/Conversation 的 Provider 调用可以 overlap。

Direct Character 如果 reaction 中产生 `GENERATE_IMAGE`，主 reaction 先完成；Visual Runtime 在后台生成并追加 IMAGE Event/SSE。

## 6. Group chat

群聊使用共享表：

```text
conversations
conversation_members
conversation_events
conversation_runtime_traces
```

同一条房间消息只存一次。每个成员读取同一事实，但可以形成不同 Memory 和 Reaction。

### Member ordering

Group member 当前按顺序反应：

```text
member A -> commit visible actions
member B -> sees updated shared history -> decide
member C -> ...
```

这保留角色之间的公开因果关系，因此不直接并行所有成员。

**成本形状**：一条 user message 会让**每个成员各产生一次模型调用**（`group_conversation_service.py` 的 user-turn 循环没有提前 break），而自主机会有硬上限（`cap = 1..4`）。群成员上限是 `MAX_GROUP_CHARACTERS = 12`，所以满员群里一句用户消息最坏是 12 次模型调用 + 12 条回复；这不是 bug，但改动群上限或给群加人时要按这个数量级算成本。

`group_character_event` 在每个成员提交后立刻 SSE 推送，不需要等整个群组结束才展示第一条回复。

### Ensemble groups（一键建群）

`POST /v1/ensembles` 只创建一个不可见的 `BUILDING` build record，不提前创建真实 GroupConversation；`POST /v1/ensembles/prepare` 是一键入口，会创建 build record 后执行联网 research。失败时 build 保留为可重试状态，不再因为一次 Provider / structured-output 错误把整次输入和进度删掉。兼容的 `/{build_id}/research` 可继续对已有 build 重试资料整理；单个失败成员还可以通过 `/{build_id}/members/{index}/retry` 局部恢复。

Research 只负责整理群体事实。成员 Persona 不再为每个人额外发起一次严格 JSON LLM 调用，而是从 `EnsembleMemberResearch` 做确定性 projection。年龄是弱资料：允许 `18`、`18岁（大学一年级）`、`年龄不详` 或 null；只有能可靠抽出 1..120 的整数时才进入 `PersonaDraft.age`，否则保持 null。角色 identity、description、personality、speech style、relationship 才是建模和后续声线设计的主要输入。

成员整理采用 partial-success：一位成员格式异常只标为 `FAILED` 并保留 research 原始字段，其他可用成员继续；只要至少 2 位成员是 `READY`，整个 build 就可以进入确认页。前端默认隐藏内部 Pydantic/Provider 细节，用户只看到可理解的“重试这一位 / 重试整理 / 修改描述”。

`/confirm` 由用户勾选后才创建或复用 Character，并在确认成功时创建真实 GroupConversation、把 build re-key 到真实 group id；`/cancel` 放弃未激活 build。confirm **不会自动开聊**——它只建角色、写成员并进入正常群聊生命周期，进群后仍需用户自己发第一句。

可选 `use_voice_design=true` 只在用户显式勾选后生效。它要求用户已经手动启动 Qwen3 VoiceDesign sidecar；群聊和 Character 核心 commit 完成后，后台才按角色 identity/personality/speech style 逐个尝试 VoiceDesign + freeze。VoiceDesign 未启动、不可用或单个角色生成失败都只记日志并保留现有默认/回退 voice，绝不回滚 Character 或 Group。

模型调用量级因此从“1 次群体 research + 每名成员 1 次 Persona structured call”收敛为主要的群体 research 调用；成员 Persona projection 为本地确定性转换。受角色容量约束：软阈值 10 位、硬上限 20 位，由 API 强制（`api.py` 的 `SOFT_ACTIVE_CHARACTERS` / `MAX_ACTIVE_CHARACTERS`），超过硬上限整批拒绝而不是截断。

### Member failure isolation

一个成员 structured output/provider 失败：

```text
A success
B failure -> member-local error/silence
C continues
```

不会因为 B 的一次失败把后面成员全部吞掉。

如果这一轮所有成员都失败，才提升为 group-level reaction error，避免把系统性故障伪装成“大家都沉默”。

### Autonomous Group Chat

已有群聊现在可以在没有新 User message 的情况下获得稀疏的自主交流机会。它仍然复用同一组 `conversation_events`、Person context、Memory/Mental State 与 Group SSE，不存在“群聊专属人格”。

正式调度由 `GroupAutonomyScheduler` 驱动，状态写入：

```text
group_autonomy_state
group_autonomy_runs
```

默认 baseline：

```text
interval          360 min
max messages      3
user quiet guard  30 min
poll              60 s
```

一次 Opportunity 的行为边界：

```text
hidden GROUP_OPPORTUNITY fact
  ↓
rotating seed member decides
  ├─ silence -> whole opportunity ends
  └─ speaks
       ↓
remaining members each judge at most once
       ↓
hard cap 1..4 visible character messages
```

- seed 每获得一次 Opportunity 就轮换一个成员，游标是该 Group 已有 Opportunity 的计数，不是事件 id——事件 id 每轮前进 `1 + 本轮消息数`，在成员数正好等于上限的群里会原地打转；
- seed 沉默时不会为了 KPI 唤醒其他成员；
- 每个成员本轮最多一个可见动作；
- V1 允许 MESSAGE / VOICE_MESSAGE / EMOJI / STICKER / 已有 IMAGE；
- V1 主动群聊明确不允许 GENERATE_IMAGE，避免复用“最新 User watermark”生图 stale contract 时产生语义冲突；
- 新 User Event 可以在模型生成期间持久化，commit guard 会把过时的自主结果标为 `SUPERSEDED`；
- 归档 Character 不参与新的自主交流；归档 Group 不参与调度；
- `GROUP_OPPORTUNITY` 是 hidden provenance，不进入正常历史、搜索或人物 Recent Events；
- 自主 Character message 仍通过现有 `group_character_event` SSE 推送；
- VOICE_MESSAGE 继续交给现有 VoiceMessageMaterializer，更新同一 conversation event。

Dev Console 可以手动触发 Opportunity、强制 due、调 interval/max messages/quiet guard/poll。手动触发为方便验收不强制 quiet guard；正式 Scheduler 会遵守。

### Group autonomous ImageGen

Group 中每个 Character 都可以在自己的用户消息 reaction 中独立决定是否输出：

```json
{
  "type": "GENERATE_IMAGE",
  "image_purpose": "SELFIE",
  "visual_intent": "..."
}
```

它不是“群聊另建一套生图系统”。当前复用：

- `VisualPromptPlanner`
- Image Provider abstraction
- MediaStorage
- `SELFIE / SCENE`
- avatar reference
- stale-result guard

生成完成后，以对应 Character 身份写入：

```text
conversation_events
```

并通过已有 `group_character_event` SSE 推送。

主文本/状态先提交，ImageGen 是 secondary asynchronous event；Provider 失败不会回滚主 reaction。

### Conversation Archive

群聊支持 soft archive/restore。归档不是删除事实。

SQLite `conversations` 保存：

```text
archived_at
archived_at_epoch
```

默认 `GET /v1/groups` 只返回活跃群聊，`GET /v1/groups?archived=true` 返回归档列表。

归档不会删除：

- `conversation_events`
- `conversation_runtime_traces`
- Character 已形成的 Memory
- MediaAsset / 本地媒体文件

Archive 与 group reaction 使用同一 per-group lock，避免形成半提交状态。

## 7. Group Mentions

Mention 是**注意力与顺序信号**，不是独占路由权限。

Durable metadata 保存 Character ID：

```json
{"mentions":["rei","momo"]}
```

`["*"]` 表示 `@所有人`。

`@Rei` 时：

- Rei 优先判断；
- Context 明确告诉 Rei 被点名；
- 其他成员仍然可以独立决定是否自然回应；
- 即使被点名，`actions=[]` 仍合法。

多个 Mention 按出现顺序领先，其余成员再按普通 room order 判断。

## 8. User media and Visual Capture

### Durable image attachment

文件选择、Clipboard paste、AI generated draft 最终都复用统一 image-send contract：

```text
browser data URL
  ↓ validation/sniff
MediaStorage local file
  ↓
media_assets metadata
  ↓
USER_MESSAGE / conversation Event references media_id
  ↓
current turn can use Vision
```

Base64 不进入 Event/Trace 数据库。

显式 AI 生图工具默认只把结果放进前端 draft；用户最终按发送后，才和普通粘贴图片一样成为聊天事实。

### Transient Camera / Display Capture

Visual Capture：

```text
Camera / Display stream
  ↓ browser keyframe sampling
1..5 selected frames
  ↓
visual message route
  ↓
Event stores metadata only
  ↓
frame data URLs passed to this reaction
```

限制：

- 最多 5 帧；
- 单帧 `<= 2 MiB`；
- 总计 `<= 6 MiB`；
- JPEG / PNG / WebP；
- Direct / Group 都支持。

Event metadata 只保存类似：

```text
frame_count
sources
captured_at_ms
```

frame bytes 不作为长期聊天附件。

详见 [`VISUAL.md`](VISUAL.md)。

## 9. Voice transcript gate

Browser Voice 的 ASR transcript 在创建聊天事实前先做最小有效性过滤：

```text
trim 后空字符串        -> reject
纯符号/标点             -> reject
任意汉字                 -> accept
ASCII Latin/digit >= 2  -> accept
其它                     -> reject
```

无效 transcript 不会发送 chat message，也不会因为当前通话开启了 Camera/Screen 而上传 Visual Capture frame；UI 回到 listening。

这个 gate 只是防明显垃圾 ASR，不是 NLP 语义判定器。

## 10. Message search

Search 只查真实 durable chat facts：

- Direct：`events` 中 USER/CHARACTER message；
- Group：活跃 `conversations` 中 USER / CHARACTER 的 `conversation_events`；hidden SYSTEM Opportunity 不进入搜索。

归档 Group 默认不进入普通 message search；恢复后自动重新进入搜索范围。底层 Event 没有删除。

不搜索：

- Memory
- Mental State
- Runtime Trace
- Intent
- Diary

Baseline 使用参数化 SQLite `LIKE`。数据量真正证明 full scan 不够时，再考虑 FTS5，不提前改变 API contract。

## 11. Timestamp UX

Direct / Group 聊天消息共享：

```text
web/time_format.js
```

当前消息时间显示：

```text
MM-DD HH:mm:ss
```

日期 separator 保持原有逻辑，不因为消息 timestamp 增加月日而删除。

## 12. Typing indicator semantics

“正在输入中”是 reaction runtime 的 UI 状态，不是人物真的在逐字键盘输入。

它可能持续较久的正常原因：

- cloud LLM latency；
- structured output repair；
- 群成员 sequential reasoning；
- Vision turn；
- current reaction burst/window。

它不应该因为页面切换而永久残留；fresh SSE status snapshot 负责重新校正。

## 13. Failure model

优先级：

```text
Durable user fact
  > outward reply
  > optional memory/intent metadata
  > optional slow visual output
  > ephemeral UI status
```

因此：

- 用户 Event 一旦接受，不因 LLM 失败消失；
- outward action 合法时，辅助 candidate 的小格式错误应局部降级；
- outward action 自己 malformed 时仍需要 repair/failure；
- 一个 group member 失败不应该结束整个 room turn；
- ImageGen 等慢工具失败不应该反向撤销文本回复；
- SSE 丢一个 transient UI event 不应该破坏 durable history；
- Camera/Screen frame bytes 丢失不等于 durable User Event 被删除。

## 14. Scaling boundary

当前 Scheduler/SSE hub 是进程内对象。

SQLite 虽然 durable，但 pending reaction queue、SSE sequence 与 worker state 不跨进程共享。因此当前不要启用多个 Character Runtime app workers 期待自动获得正确 reaction scheduling。

真正需要 multi-worker/remote deployment 时，再设计：

- durable queue
- shared pub/sub
- idempotent reaction jobs
- cross-process watermark ownership

## Voice Messages

Voice Message 属于 Direct / Group Conversation contract：它是 durable message expression，不是 live call transport。底层 ASR/TTS Provider 与 Workbench 由 [VOICE_AND_TTS.md](VOICE_AND_TTS.md) 统一维护。

Status: **V1 is implemented on current `main` for Direct and Group chat.**

Voice Message is a durable chat expression, not a live call transport. The text remains the canonical message body; synthesized audio is an attached MediaAsset that can fail without deleting the message.

### Current flow

```text
PersonReaction VOICE_MESSAGE
  -> persist CHARACTER_MESSAGE with text + voice_status=pending
  -> publish pending event
  -> VoiceMessageMaterializer
  -> POST Media Runtime :8001/v1/tts with the complete message text
  -> save WAV/MP3 through MediaStorage
  -> update the same event id
       ready  -> voice_media_id + optional duration
       failed -> voice_error, text remains readable
  -> republish the same Direct/Group event id over SSE
  -> browser merges the update in place
  -> compact voice bubble playback + always-visible text underneath
```

Canonical metadata is defined in `character_memory.voice_message_fields`:

```text
voice_status
voice_media_id
voice_duration_ms
voice_error
```

The state transition is:

```text
pending
  ├─ ready
  └─ failed
```

A failed TTS provider never removes the message text. The failure reason carried by the provider chain is preserved in `voice_error` and surfaced by the browser bubble.

### Direct and Group

Direct events live in `events`; Group events live in `conversation_events`. Both use the same `VOICE_MESSAGE` action contract and formal TTS path.

Direct `TIME_TICK` wakes and due `PROACTIVE_INTENT` turns may also choose `VOICE_MESSAGE`. They persist the same pending character event and reuse the same direct publication/materialization hook, so background speech is synthesized instead of remaining a text-only or permanently-pending voice action. Autonomous Group Chat already follows the Group materializer path.

The scheduler/materializer updates the **original event id** instead of appending a second message. Browser Direct and Group history/SSE projections preserve the voice fields and merge by id.

### Relationship to voice calls

```text
Voice call
  microphone -> ASR -> normal Person reaction -> ephemeral playback pipeline

Voice message
  Person reaction -> durable text event -> synthesize once -> persisted audio -> replay later
```

They may use the same configured TTS provider, but a durable Voice Message does not depend on live-call UI state.

Chat dictation (`web/dictation.js`) and a voice call exclude each other: only one microphone capture is open at a time. Dictation refuses to start while a call is live, and both starting a call and switching conversation retire a dictation start that is still waiting on the permission prompt — the late "Allow" is never adopted, its tracks are stopped on the spot and the button returns to idle. A recording that is already live is dropped on the same two events without being sent to ASR, because its transcript would otherwise land in the conversation the user has already left. The permission prompt may be answered at any point after the request, so *holding a stream* and *still wanting this capture* are two separate things, and the answer is judged against what was wanted when the request was made.

### Boundaries

- One `VOICE_MESSAGE` is one complete TTS request; no sentence/chunk splitting in V1.
- Ordinary `MESSAGE` remains text-only and is not automatically materialized.
- Audio is MediaAsset data, not Memory.
- Every Voice Message shows its canonical text directly below the voice bubble. There is no WeChat-style “tap to convert/show text” step and no placeholder “翻译” button.
- The always-visible line is the canonical spoken text/transcript, not a separate machine-translation backend contract.
- Space Voice Post is implemented as a separate social-channel media intent: one complete `VOICE` intent synthesizes through the same formal `:8001/v1/tts` route and persists as a Space MediaAsset. Its stored transcript is also shown directly below the voice bubble. It does not reuse chat `VOICE_MESSAGE` event semantics.
- Raw microphone audio remains a transport/input concern and is not persisted as character memory by default.

### Main modules

```text
domain/models.py
    VOICE_MESSAGE action contract

voice_message_fields.py
    canonical persisted metadata keys/defaults

application/voice_message_materializer.py
    formal TTS -> MediaAsset -> ready/failed

application/voice_message_service.py
    durable state transitions + SSE republish

application/async_conversation.py
    Direct/Group scheduling hook

web/app.js
web/groups.js
    Direct/Group voice bubble projection/playback

space_media_executor.py
web/space.js
    autonomous Space VOICE synthesis + durable media relation + native playback
```

Regression coverage lives in `test_voice_message_*.py`, including persistence, materialization, Direct/Group transition and browser contract tests.

## Stickers

Sticker 属于聊天/社交表达资源。当前 runtime catalog、global ownership 与 legacy compatibility contract 收敛在 Conversation Runtime 中；Space 使用同一资源语义，社会层行为见 [SOCIAL_WORLD.md](SOCIAL_WORLD.md)。

Sticker 当前是 **application/global resource**，不是“每个 Character 独占一套资源”的新设计。

运行时仍兼容历史 character-local pack，因此必须区分：

```text
current ownership   = global application resource
legacy compatibility = persona-local manifests / old character-scoped routes
```

V1 保留兼容，不为了清理历史语义去破坏现有资源。

### 1. Runtime catalog

正式 runtime catalog 会合并：

```text
built-in default pack
+
global user-imported pack(s)
+
legacy character-local manifests
```

核心入口：

```text
load_global_sticker_catalog(...)
```

当前 source 描述为：

```text
default+global+legacy
```

同一个 global catalog 会刷新到 Direct / Group 使用的 PersonRuntime，不要求每个 Character 重新复制一份 imported sticker。

### 2. Storage

全局用户 Sticker 默认存放在配置的：

```yaml
sticker_dir: ""
```

为空时从 DB parent 推导默认目录。

全局 manifest：

```text
<sticker_dir>/manifest.yaml
```

内置 default pack 位于 package Web assets；legacy persona pack 仍可能位于：

```text
<persona_dir>/stickers/manifest.yaml
```

这些 legacy manifests 只是兼容输入，不改变当前 global ownership。

### 3. Public HTTP surface

#### Catalog

```text
GET /v1/stickers
```

可选 `character_id` 仍被接受用于旧客户端兼容，但正式返回：

```json
{
  "scope": "global",
  "source": "default+global+legacy",
  "stickers": []
}
```

用户导入资源在 Direct / Group 中共享。

#### Global asset

```text
GET /v1/stickers/{sticker_id}/asset
```

这是当前正式 asset route。

#### Legacy asset route

```text
GET /v1/stickers/{character_id}/{sticker_id}/asset
```

仍保留给旧客户端，但读取的仍是当前 global catalog。不要据此重新把 Sticker ownership 解释成 character-owned。

### 4. Web ZIP import

正式 Web import：

```text
POST /v1/stickers/import
Content-Type: application/zip
```

兼容参数：

```text
character_id
filename
auto_tag
```

其中 `character_id` 即使由旧客户端发送，也**不会决定 storage ownership**。后端只用它做兼容校验；导入仍写全局 user library。

成功后 runtime 会重新加载 global catalog，并刷新已经加载的人物 Sticker resource。

### 5. Import metadata

导入 ZIP 优先读取：

```text
all_tags.json
```

或者一个/多个：

```text
tags.json
```

metadata row 可以提供：

- id
- filename/file
- 中文/英文标签
- aliases/tags
- description
- set/pack id
- display/pack name

如果 ZIP 没有 metadata：

```text
auto_tag=true + available AI tagger
  -> 可以对图片自动生成 label/tags/description

auto_tag=false
  -> reject
```

即使已有 metadata，字段语义不完整时也可以按需用 AI tagger 补齐。

AI tagging 是 import-time metadata enrichment，不是每次 Character 想发 Sticker 时再调用一个模型。

### 6. Import safety

导入器有明确限制：

```text
archive bytes       <= 64 MiB
uncompressed bytes  <= 160 MiB
files               <= 500
supported assets    png/webp/gif/svg/jpg/jpeg
```

ZIP member 会做 path traversal 防护，不接受 absolute path 或 `..` escape。

导入采用 validate-first + manifest-last publication：

```text
read/resolve/tag/validate all rows
        ↓
write immutable/content-addressed assets
        ↓
validate temporary manifest
        ↓
atomic replace manifest last
```

目标是避免中途失败后，runtime 看到“manifest 已更新但图片还没写完”的半导入状态。

如果最终 commit 失败，新创建但未被正式 manifest 引用的资产会尽量回滚。

### 7. Runtime selection

PersonRuntime 不允许模型凭空发任意 Sticker ID。

每轮：

```text
global catalog
  ↓
Sticker retrieval / available resources
  ↓
LLM may choose one real sticker_id
  ↓
resource validation
  ↓
STICKER action
```

未知、不存在或 asset file 丢失的 Sticker 不应该被当作合法 outward resource。

模型看到的是有限的可用资源及其语义标签，而不是整个文件系统。

### 8. Built-in vs imported vs legacy

#### Built-in

随项目提供的 default pack，作为所有人物的基础资源。

每个内置 SVG 都带 `width`/`height`（与 `viewBox` 同为 160），因为它们只给 `viewBox` 时没有 intrinsic width：消息气泡里的 `img` 会先按 0×0 布局、再被 shrink-to-fit 的容器框住，贴纸于是渲染成时间戳的宽度而不是气泡的上限。CSS 侧另有显式尺寸盒（`--sticker-size`），两者合起来保证贴纸在私聊和群聊里是同一个大小。

#### Global imported

当前正式用户扩展资源。Web import 和 CLI import 都写到 `sticker_dir`，所有人物/群聊共享。

#### Legacy character-local

早期 persona-local manifest 仍会被 runtime 合并，避免已有资源突然消失。

这是兼容层，不是新资源应该继续采用的 ownership 模式。

### 9. CLI import compatibility

`sticker_import_cli.py` 当前已经与 global ownership 对齐：

```text
archive ZIP
  -> resolve_sticker_dir(settings)
  -> global manifest/assets
  -> load_global_sticker_catalog(...)
```

历史 `--character` 参数仍接受，避免已有本地脚本直接失效，但它只做 character id 兼容校验，并打印 deprecated 提示；**不会改变 storage ownership**。

因此当前事实源保持一致：

```text
Web import  -> global
CLI import  -> global
Runtime     -> default + global + legacy read compatibility
```

legacy persona-local manifests 继续只读兼容，不再作为新 CLI 导入目标。

### 10. Relationship to ImageGen

Sticker 与 ImageGen 是不同资源路径：

- `STICKER` action 选择已经存在的 Sticker resource；
- `GENERATE_IMAGE` 触发新的图片生成；
- `VisualPurpose.STICKER` 只是 provider contract 中保留的 purpose，不代表当前聊天会自动用 ImageGen 即时制造每个 Sticker。

如果未来要做“AI 现场生成 Sticker”，需要单独定义生成、审核、入库和复用语义，不能直接混进现有 Sticker retrieval。

### 11. Regression expectations

至少持续覆盖：

- built-in/global/legacy catalog merge；
- `/v1/stickers` 返回 global scope；
- legacy `character_id` 不改变 Web/CLI import ownership；
- CLI `--character` compatibility 不写回 persona-local library；
- asset path validation；
- ZIP size/file-count/path traversal 限制；
- metadata-present 和 AI-auto-tag 两类 import；
- manifest-last atomic publication；
- runtime catalog refresh；
- unknown Sticker ID 不成为合法 outward action；
- legacy asset route 继续兼容；
- 贴纸在私聊和群聊中渲染为同一个显式尺寸盒（`--sticker-size`），不随容器 shrink-to-fit 缩水。

相关回归清单见 [`EVALS.md`](EVALS.md)。
