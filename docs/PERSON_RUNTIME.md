# Person Runtime

本文只描述人物在收到 Event 后如何产生内部变化和外部行为。

## 1. 核心模型

普通 chatbot 往往是：

`Input -> Must Output`

Persistent Person 使用：

`Event -> Perceive -> Recall -> React -> Mental Update -> Decide -> Express? -> Persist`

核心接口保持简单：

```python
result = person_runtime.handle(event)
```

用户消息、时间事件和主动意图都走同一个 Person Runtime。

## 2. Reaction always exists; Expression may be empty

核心原则：

**每个被感知的事件都可以形成内部 Reaction，但不是每个 Event 都必须产生外部回复。**

P0 主 contract 是：

```text
PersonReaction
├── perception              # 可选安全摘要
├── reaction                # 可选安全摘要
├── mental_state_update     # 可选；空=沿用旧状态
├── actions[0..3]           # 主要对外行为
├── memory_candidates[]
└── intent_candidates[]
```

`actions` 当前只需要承担自然聊天表达：

- `MESSAGE`
- `EMOJI`

`actions=[]` 就是真正的沉默，不会落一条假的 Character Message。

旧的 `REPLY / MINIMAL_RESPONSE / NO_REPLY / DEFER / PROACTIVE_MESSAGE / NO_ACTION` 仍保留用于旧 Trace、冻结子系统和兼容调用，但新聊天主路径不需要模型主动生成旧 `action` 字段。

一次最多 3 个 Action 是产品护栏，不要求模型每轮拆成多条；默认一条仍然完全正常。

## 3. Silence / refusal to reply

沉默是一等行为，不是异常路径。常见原因：

- 对话已经自然结束。
- 对方明确说“不用回复”。
- 不知道如何自然回应。
- 认为对方需要空间。
- 当前没有想说的话。
- Persona 本身不愿意回应或不想展开。

规则是：

**Every silence has a reason, but not every reason must be disclosed.**

结构上不再通过强制 `action` 暗示模型“收到消息就必须回”。对于当前聊天主路径，真正不回复就是：

```json
{"actions": []}
```

WebUI 可以显示轻量的 `已读 · 没有回复`，但这不是人物发送的一条消息。

## 4. Multi-action expression

真实聊天允许一轮出现自然的连续表达，例如：

```json
{
  "actions": [
    {"type": "MESSAGE", "message": "诶？？"},
    {"type": "EMOJI", "message": "🥺"},
    {"type": "MESSAGE", "message": "怎么回事呀？"}
  ]
}
```

这些 Action 会按顺序分别持久化为 `CHARACTER_MESSAGE`，共享同一个 `source_event_id`，并保存 `action_index`。

不要把每句话机械拆开；多 Action 的价值是允许自然停顿、补一句、表情和追问，而不是制造刷屏。

## 5. Mental State

Mental State 默认是语言，不做核心 RPG 数值：

```text
今天有些疲惫，不想展开很长的对话。
仍然记得用户下午的汇报，有一点好奇结果。
```

当前语义：

- `mental_state_update` 有内容：它是事件后的完整紧凑状态，替换旧状态。
- `mental_state_update=""`：本轮没有值得持续的心理变化，沿用上一状态。

Event Log 才负责保存历史变化。

## 6. Relationship Time / Re-encounter

每次聊天前记录最近一次聊天事件，并把关系时间传入 Context：

```text
# Relationship Time
- 上次聊天时间：...
- 距离上次聊天：7 天
```

人物自己根据 Persona、Memory 和当前语境决定要不要提旧事。

禁止写死 `gap > N -> 好久不见` 这类机械规则。

## 7. Intent 与主动行为

Intent 表示人物曾经形成但未必立刻执行的行为倾向。

基本状态：

- `PENDING`
- `EXECUTED`
- `DEFERRED`
- `SUPPRESSED`
- `EXPIRED`

当前 P0 不扩展 Tool Agent；已有 Life/Tick/Intent 保持兼容和冻结状态。未来若恢复主动行为建设，也应继续经过同一个 Person Runtime，而不是独立 Engagement 系统。

## 8. 用户可见“想法”与 Developer Trace

普通用户可以选择查看：

- 简短 `perception`
- 简短、安全的 `reaction`

它们是人物化的**安全思考摘要**，不是 raw chain-of-thought，也不是完整私密心理过程。

Developer Trace 继续可展示：

- Recall 到了哪些 Memory。
- `perception / reaction`。
- Current Mental State。
- `actions[0..3]`。
- Memory admission decision。
- Intent。
- 实际 model messages / raw structured response。
- 各阶段 timings。

任何时候都不把模型隐藏 chain-of-thought 当成产品数据保存或展示。
