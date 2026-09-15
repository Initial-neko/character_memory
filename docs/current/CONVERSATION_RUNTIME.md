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

`group_character_event` 在每个成员提交后立刻 SSE 推送，不需要等整个群组结束才展示第一条回复。

### Member failure isolation

一个成员 structured output/provider 失败：

```text
A success
B failure -> member-local error/silence
C continues
```

不会因为 B 的一次失败把后面成员全部吞掉。

如果这一轮所有成员都失败，才提升为 group-level reaction error，避免把系统性故障伪装成“大家都沉默”。

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

详见 [`VISUAL_CAPTURE.md`](VISUAL_CAPTURE.md)。

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
- Group：活跃 `conversations` 的 `conversation_events`。

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
