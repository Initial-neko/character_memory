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

已经确认的原则：

**每个被感知的事件都产生内部 Reaction，但不是每个 Reaction 都必须产生回复。**

V0 Action：

- `REPLY`
- `MINIMAL_RESPONSE`
- `NO_REPLY`
- `DEFER`
- `PROACTIVE_MESSAGE`
- `NO_ACTION`

因此“沉默”不是异常路径，而是正式行为之一。

## 3. Silence

沉默必须有内部可解释原因，例如：

- 对话已经自然结束。
- 不知道如何自然回应。
- 认为对方需要空间。
- 当前不想展开长对话。
- 某件事仍然介意，因此选择不表达。

规则是：

**Every silence has a reason, but not every reason must be disclosed.**

系统层可以明确 delivered/seen 等状态；人物层不应该暴露数值好感度、隐藏 chain-of-thought 或完整私密心理过程。

## 4. Mental State

Mental State 默认是语言，不做核心 RPG 数值：

```text
今天有些疲惫，不想展开很长的对话。
仍然记得用户下午的汇报，有一点好奇结果。
昨天突然结束的话题还稍微有些介意。
```

当前 `mental_state_update` 的语义是：**返回事件发生后的完整、紧凑当前状态，并替换旧状态**。它不是无限增长的日志。

Event Log 才负责保存历史变化。

## 5. Intent 与主动行为

Intent 表示人物曾经形成但未必立刻执行的行为倾向。

基本状态：

- `PENDING`
- `EXECUTED`
- `DEFERRED`
- `SUPPRESSED`
- `EXPIRED`

Time Tick 会重新处理到期 Intent。主动消息不是单独的“营销系统”，而是同一个 Person Runtime 在没有用户新消息时选择 `PROACTIVE_MESSAGE`。

这保证：主动联系也必须由 Persona、Memory、Mental State 和真实未完成事情共同解释。

## 6. Developer Inspector 可见什么

可展示：

- Recall 到了哪些 Memory。
- 简短 `perception`。
- 简短、安全的 `reaction` summary。
- 当前 Mental State。
- 最终 Action。
- 一句 `action.reason`。
- 新增 Memory / Intent。

这些是结构化的可解释状态，不是模型隐藏 chain-of-thought。
