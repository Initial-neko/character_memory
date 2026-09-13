# P0.6 — Proactive Messaging & Unread

本阶段把已有 Intent 能力接成最小可用的产品闭环：人物在有明确、持久化 Intent 时可以主动发消息；用户不在当前人物会话时，左侧出现未读红点和最新消息预览。

## 1. 产品原则

主动消息不是 engagement 机制。

允许的来源：

- 之前共同经历产生的后续关心；
- 明确未来事件，例如“明天下午汇报后问结果”；
- Persona / Memory / Mental State 下自然形成的 Intent。

不做：

- “用户 N 小时没打开就发消息”；
- 空闲时反复 TIME_TICK 询问模型要不要联系用户；
- 连续多条主动消息催促用户。

## 2. 调度路径

服务器后台每 30 秒做一次轻量 SQLite 检查：

```text
PENDING Intent
  ↓ earliest_at <= now <= expires_at
cheap SQLite check
  ↓ only when due exists
lazy Runtime init if needed
  ↓
ProactiveService
  ↓
ChatService per-character lock
  ↓
PROACTIVE_INTENT Event
  ↓
PersonRuntime
  ├── Recall
  ├── Persona
  ├── Mental State
  ├── actions[0..3]
  ├── Memory Admission
  └── Trace
  ↓
0..3 Character Messages or silence
```

没有 due intent 时，不初始化模型，不调用 LLM。

## 3. Intent 状态

当前 P0.6 使用：

- `PENDING`
- `PROCESSING`
- `EXECUTED`
- `SUPPRESSED`
- `DEFERRED`
- `EXPIRED`
- `ERROR`

Provider/Runtime 异常时标记 `ERROR`，避免后台无限重复调用同一个 Intent。

## 4. 防骚扰规则

如果某人物最近一条聊天消息本身来自 `PROACTIVE_INTENT`，且用户之后没有回复，则暂不继续执行该人物的其他主动 Intent。

即：

```text
人物主动消息
  ↓
等待用户回复
  ↓
用户回复后，后续 Intent 才重新有机会执行
```

同时每次 poll 每个 Character 最多执行 1 个 Intent，避免多个到期 Intent 在同一轮形成消息爆发。

## 5. Provenance

新 Character Message metadata 保留：

```text
source_event_id
source_event_type
conversation_id
action
action_index
```

其中主动消息使用：

```text
source_event_type = PROACTIVE_INTENT
```

因此前端不需要依赖 `action == PROACTIVE_MESSAGE` 猜测消息来源；即便模型主 contract 返回 `MESSAGE / EMOJI`，仍能可靠标识主动消息。

## 6. Conversation

主动 Intent 重新判断时优先复用该 Character 最近聊天事件中的 `conversation_id`。

若从未存在 conversation，则使用：

```text
<character_id>:proactive
```

无论 session 如何选择，PersonRuntime 仍会看到持久化 Event / Memory / Relationship Time。

## 7. Unread / Red Dot

WebUI 每 5 秒读取轻量 Character Summary：

- latest chat message；
- latest assistant message id。

V0 仍是本地单用户，因此 Read State 暂存在浏览器 `localStorage`：

```text
character-memory:last-read:<character_id>
```

判断：

```text
latest_assistant_message_id > last_read_id
  => unread red dot
```

点击人物后立即 mark read；当前正在打开的人物收到新消息时直接刷新聊天并 mark read，不显示自己的红点。

第一次启用 P0.6 时，会把已有历史消息建立为 read baseline，避免升级后所有旧会话同时出现红点。

## 8. Character List Preview

左侧人物项现在优先显示最近一条聊天预览：

```text
Neko        ●
忙完了吗？

Momo
你：晚点再聊
```

没有聊天记录时继续显示 Persona tagline / identity。

## 9. 当前边界

本阶段不做：

- 系统级桌面/手机 Push Notification；
- WebSocket / SSE；
- 多设备 Read State 同步；
- Relationship State；
- 空闲 TIME_TICK 主动性生成；
- 主动消息数量优化或复杂频控模型。

V0 当前使用 SQLite dispatcher + WebUI polling，先验证“人物会不会在真正有理由的时候主动回来找用户”这一核心体验。
