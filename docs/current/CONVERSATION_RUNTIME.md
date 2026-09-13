# Conversation Runtime

本文汇总当前 Direct / Group Chat 的事实存储、异步反应、SSE、搜索和 Mention contract。历史 P0.15/P0.16 文档已归档，不再作为当前入口。

## 1. User message first, reaction later

WebUI 当前发送用户消息时使用：

```text
POST /v1/chat/messages
POST /v1/groups/{conversation_id}/messages
```

服务端先：

1. 校验文本/Sticker/Image；
2. 保存媒体（如有）；
3. 持久化用户 Event；
4. enqueue reaction；
5. HTTP 202 返回。

用户已经发送的事实不等待 LLM 完成。

Character reaction 由 `ReactionScheduler` 异步处理，通过 SSE 推给前端。

旧同步接口仍可能服务 CLI/兼容测试，但不是正式 WebUI 主路径。

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
- 消耗掉尚未处理的 image/mention signal。

这是为了避免“用户已经补充一句话，角色几秒后还把上一版理解硬发出来”。

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
3. 服务端会额外发送当前 Scheduler 的 authoritative `reaction_status` snapshot。

这样用户切换人物/群聊/Tab 时，即使错过了旧 `idle` event，也不会让前端本地的“正在输入中…”永久残留。

真正的 EventSource 网络自动重连则继续使用 `Last-Event-ID` 尽量补 transient gap。

## 5. Direct chat

Direct Event 存在 core `events` 表中，以 `character_id` 区分人物，并通过 metadata 保留 `conversation_id`。

同一人物 turn 通过 ChatService lock 串行，避免同一个 Character 的 derived state 交叉提交。

不同 Character/Conversation 的 Provider 调用可以 overlap。

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

不会因为 B 的一次冒失把后面成员全部吞掉。

如果这一轮所有成员都失败，才提升为 group-level reaction error，避免把系统性故障伪装成“大家都沉默”。

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

Mention signal 参与同一 burst/supersession 生命周期，不会因为旧 generation 被 supersede 就提前丢失。

## 8. User media in conversations

文件选择、Clipboard paste、AI generated draft 最终都复用统一 image-send contract。

用户图片：

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

AI 生成工具默认只把结果放进前端 draft；用户最终按发送后，才和普通粘贴图片一样成为聊天事实。

## 9. Message search

Search 只查真实 durable chat facts：

- Direct：`events` 中 USER/CHARACTER message；
- Group：`conversation_events`。

不搜索：

- Memory
- Mental State
- Runtime Trace
- Intent
- Diary

当前两种模式：

- current conversation
- global direct + group

Baseline 仍使用参数化 SQLite `LIKE`，用户输入中的 `%/_` 等会按 literal 处理。数据量真正证明 full scan 不够时，再考虑 FTS5，不提前改变 API contract。

搜索结果带 deep-history navigation 信息，前端复用现有 history pagination/window，不建立第二套历史数据模型。

## 10. Typing indicator semantics

“正在输入中”是 reaction runtime 的 UI 状态，不是人物真的在逐字键盘输入。

它可能持续较久的正常原因：

- cloud LLM latency；
- structured output repair；
- 群成员 sequential reasoning；
- Vision turn；
- current reaction burst/window。

它不应该因为页面切换而永久残留；fresh SSE status snapshot 负责重新校正。

## 11. Failure model

优先级：

```text
Durable user fact
  > outward reply
  > optional memory/intent metadata
  > ephemeral UI status
```

因此：

- 用户 Event 一旦接受，不因 LLM 失败消失；
- outward action 合法时，辅助 candidate 的小格式错误应局部降级；
- outward action 自己 malformed 时仍需要 repair/failure；
- 一个 group member 失败不应该结束整个 room turn；
- ImageGen 等慢工具失败不应该反向撤销文本回复；
- SSE 丢一个 transient UI event 不应该破坏 durable history。

## 12. Scaling boundary

当前 Scheduler/SSE hub 是进程内对象。

SQLite 虽然 durable，但 pending reaction queue、SSE sequence 与 worker state 不跨进程共享。因此当前不要启用多个 Character Runtime app workers 期待自动获得正确 reaction scheduling。

真正需要 multi-worker/remote deployment 时，再设计：

- durable queue
- shared pub/sub
- idempotent reaction jobs
- cross-process watermark ownership
