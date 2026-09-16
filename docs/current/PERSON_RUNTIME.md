# Person Runtime

本文描述一个 Character 收到 Event 后，如何形成内部变化与对外行为。

## 1. 核心模型

普通 chatbot 常被写成：

```text
Input -> Must Output
```

Persistent Person 使用：

```text
Event
  -> Perceive
  -> Recall
  -> React
  -> Mental Update
  -> Decide
  -> Express? / Tool?
  -> Persist
```

核心入口保持简单：

```python
result = person_runtime.handle(event)
```

用户消息、Group shared fact、TIME_TICK、Intent 等仍复用同一个 Person Runtime，不建立彼此独立的人格状态。

## 2. PersonReaction contract

当前主 contract：

```text
PersonReaction
├─ perception              optional developer-safe summary
├─ reaction                optional developer-safe summary
├─ mental_state_update     optional; empty = keep current state
├─ actions[0..3]
├─ memory_candidates[]
└─ intent_candidates[]
```

当前可见表达 Action：

- `MESSAGE`
- `EMOJI`
- `STICKER`
- `IMAGE`

当前内部工具 Action：

- `GENERATE_IMAGE`

Legacy `REPLY / MINIMAL_RESPONSE / NO_REPLY / DEFER / PROACTIVE_MESSAGE / NO_ACTION` 仍保留兼容，但不是新聊天模型应优先生成的主 contract。

## 3. Reaction always exists; expression may be empty

每个被处理的 Event 都可以形成内部 reaction，但不是每个 Event 都必须产生外部回复。

合法沉默：

```json
{"actions": []}
```

常见原因：

- 对话自然结束；
- 用户明确说不用回复；
- 人物当前没有想说的话；
- Persona 认为应该给对方空间；
- 人物不愿意展开；
- 当前 Event 只值得内部更新。

WebUI 可以展示轻量 `已读 · 没有回复`，但不会伪造一条 Character Message。

模型必须**显式**返回 `actions`，即使它是空数组。缺失 `actions` 不是合法 silence，而是 structured-output contract drift，需要 repair/failure。

## 4. Structured-output resilience

主 outward action 保持严格，但辅助认知字段允许窄范围容错。

例如：

```json
{
  "actions": [{"type":"MESSAGE","message":"我知道啦。"}],
  "memory_candidates": [{"content":"...","importance":4}]
}
```

辅助 `importance=4` 会被收敛到合法区间，而不会把已经有效的 MESSAGE 一起作废。

同样：

- numeric string 可以转为数字；
- malformed optional memory/intent candidate 可以单独丢弃；
- debug summary 的 harmless scalar drift 可以字符串化。

对 outward action 只接受无歧义兼容，例如 Provider 偶尔返回：

```json
{"action":"MESSAGE","text":"你好"}
```

会归一化成：

```json
{"type":"MESSAGE","message":"你好"}
```

但不会把完全缺失 `actions` 自动猜成 `[]`。这样既减少无意义第二次 LLM 调用，又不把真正主协议错误伪装成沉默。

## 5. Multi-action expression

一轮最多 3 个 Action，例如：

```json
{
  "actions": [
    {"type":"MESSAGE","message":"诶？？"},
    {"type":"STICKER","sticker_id":"round_cat_pleading"},
    {"type":"MESSAGE","message":"你再说一遍？"}
  ]
}
```

有效可见 Action 会按顺序分别形成 Character Message，并共享同一个 source fact。

最多 3 个是产品护栏，不要求模型为了“自然”机械拆句。普通一条 MESSAGE 仍是默认情况。

## 6. Resource actions

### Sticker

Runtime 每轮只允许模型选择当前 Available Stickers 中真实存在的 ID。未知资源会被丢弃并进入 Trace。

Sticker 当前是 application/global resource：运行时可以合并内置 pack、全局导入 pack 和 legacy character-local manifest。具体 ownership 见 [`STICKERS.md`](STICKERS.md)。

### Existing Image

`IMAGE` 引用人物已存在的 Character Image Catalog 或合法 MediaAsset；模型不能生成任意 URL。

### Generate Image

`GENERATE_IMAGE` 不是可见消息，结构类似：

```json
{
  "type":"GENERATE_IMAGE",
  "image_purpose":"SELFIE",
  "visual_intent":"想自然分享一下现在的样子"
}
```

当前自主链路只接受：

- `SELFIE`
- `SCENE`

执行策略：

- Direct `USER_MESSAGE` reaction 可以自主生成；
- Group 中每个 Character 的 `USER_MESSAGE` reaction 也可以独立自主生成；
- 同一 Character 单轮最多 1 个 `GENERATE_IMAGE`；
- `SELFIE` 在 Provider 支持 reference 时使用当前头像作为 identity anchor；
- `SCENE` 不要求人物本人出镜，也不为了身份一致性机械附带 avatar reference；
- 生成运行在异步 visual worker 中；
- 主文本/状态已经提交后，即使图片 Provider 失败，也不能反向让主 reply 失败；
- 生成过程中出现更新的用户事实时，旧图片结果可以被判定 stale 并丢弃。

Direct 最终追加普通 `events` IMAGE；Group 则把结果以发起生成的 Character 身份写入 `conversation_events`，并走现有 group SSE。

Group 没有第二套 ImageGen system。它复用同一个 `VisualPromptPlanner`、Provider abstraction、MediaStorage、`SELFIE / SCENE` 和 stale-result 语义，只在 shared-room persistence/SSE 上有 adapter glue。

## 7. Mental State

Mental State 默认使用紧凑自然语言，而不是 RPG 数值：

```text
今天有些累，不太想展开很长的对话。
还记得用户下午有汇报，对结果有一点好奇。
```

语义：

- `mental_state_update` 非空：事件后的完整紧凑当前状态；
- 空字符串：本轮没有值得持续的变化，沿用旧状态。

历史变化由 Event/mental-state history 保存。

## 8. Relationship Time

Runtime 在 Context 中提供最近聊天时间和真实间隔，例如：

```text
# Relationship Time
- 上次聊天时间：...
- 距离上次聊天：7 天
```

它只是上下文信号。禁止写死：

```text
gap > N -> 必须说“好久不见”
```

人物应结合 Persona、Memory 和当前语境自己决定是否提起过去。

## 9. Memory Candidate

Person Model 可以在同一次 reaction 中返回 `memory_candidates`，不为了“记不记”额外调用第二个 LLM。

Candidate 必须再经过 Memory Admission：

```text
low value? -> skip
exact / near duplicate? -> skip
otherwise -> write
```

是否形成长期 Memory 不影响已经合法的 outward reply。

## 10. Intent / Wake

`intent_candidates` 表示未来可能行动的持久化倾向。

到时间后，已有 Intent 仍会回到同一个 Person Runtime 重新判断是否表达、延期、压制或过期。

当前还存在 process-local Character Wake：

- `proactive_wake_enabled`
- 默认约每 60 分钟一次机会
- 用于 direct character，不 wake group chat
- 不是 durable distributed job queue

Wake/Intent 当前不自动获得自主 ImageGen 权限。自主生成图像只发生在用户消息 reaction 中，避免后台无配额地消耗图片 Provider。

## 11. Multimodal input is still one Person

普通图片附件、Camera、Screen Share、Voice ASR 都只是进入同一个 PersonRuntime 的不同输入渠道：

```text
text ------------------┐
image attachment ------┤
Camera/Display frames -┤ -> same PersonRuntime
ASR transcript --------┘
```

Visual Capture 的 frame bytes 只作为当前 Vision context，不变成长期人物状态。Event 只保存必要的 capture metadata。

Voice ASR 在浏览器端有 transcript validity gate：空白、纯标点/符号、过短的单个 ASCII 字符不会创建聊天事实；汉字或至少两个 ASCII 字母/数字才被接受。

## 12. Transaction / failure boundary

Source Event 已经是事实后，Derived state 应尽量原子提交。

主 reaction 完成后，一次 derived transaction 可以包括：

- Mental State
- accepted Memory
- Intent
- 0..3 outward Character Messages
- Runtime Trace / ACTION bookkeeping

可选慢工具（例如 ImageGen）应在主事务之外运行，并通过正常 Media/Event 协议追加结果。

## 13. Safe Thought / Trace

普通 UI 的“想法”只展示：

- 简短 `perception`
- 简短、安全 `reaction`

它们不是 raw hidden chain-of-thought。

Developer Trace 可以保存/展示：

- Recall snapshot
- compiled context
- safe perception/reaction
- Mental State before/after
- actions
- resource decisions
- Memory admission
- Intent
- model messages（图片 base64 脱敏）
- raw structured response
- model/timings

不要把模型隐藏思维链作为产品数据要求或持久化。
