# Person Runtime

本文描述一个 Character 收到 Event 后，如何形成内部变化与对外行为。

## Optional Direct external-task decision

`direct_claude_tasks_enabled` (default false) and the named person's operator
sidecar `capabilities: [claude_task]` jointly enable one `DELEGATE_TASK` decision
in a normal Direct USER_MESSAGE reaction. `task` is a self-contained public
question of at most 240 characters; no Persona, Memory or chat transcript is
sent to the executor. An unauthorized, archived or malformed grant fails closed;
`claude_research` alone still grants only the existing World path. Group, Visual,
World and autonomous events do not gain Direct delegation.

PersonRuntime first decides normally, then the shared CapabilityExecutor reserves
the existing per-character daily quota/budget and invokes fresh `claude -p` once.
World's backend selector stays independent. A durable execution result and an
ACTION fact (`fact_kind=CLAUDE_TASK_RESULT`) record what actually returned. One
further normal Person reaction interprets the result; it cannot delegate again,
generate another image, propose a call, or create a future tool Intent. Ordinary
turns keep their original model-call count. A delegated turn costs the original
decision plus one result interpretation, and bounded CLI calls counted separately.

The CLI may analyze a question without tools (`MODEL_RESPONSE`). Such output is
not proof of browsing or physical execution. Only actual `world_lookup` receipts
establish fetched source scope. The visible bounded assistant response is also
stored in the durable execution result, independently of the 30-day detail file.
New user input before dispatch prevents the task; supersession after dispatch
retains its real receipt but prevents stale result interpretation/output/state.
Existing async ordering, Direct SSE, Memory admission and trace ownership remain.
Life/Execution read-only details expose the linked request and result; failures
are not converted into success or automatic retries.

## Direct call invitation Action

`CALL_INVITE` 是现有 PersonReaction 的 Direct 专用邀请 Action，填写邀请 `message` 与公开简短 `reason`。它不在 EXPRESSIVE_ACTIONS 中，不落成普通消息或已完成通话经历。Settings 默认关闭；服务端来源/许可/未决数量/既有冷却硬门在原反应事务中创建来源化邀请，非 Direct 不执行。状态与麦克风生命周期见 [Voice & TTS Runtime](VOICE_AND_TTS.md#人物发起的应用内来电邀请)。

到期表达 Intent 实际发出邀请时可以标为 EXECUTED，但 Intent 审计明确记录 `CALL_INVITATION_PROPOSED`：仅邀请已提出，不代表通话接通或聊过。生活投影以 CALL_INVITATION_PROPOSED 明示仅邀请并保留 stored_status=EXECUTED。邀请仍为 PENDING，接受/拒绝/过期属于独立状态；共同交流事实只来自真正接通后既有语音消息入口。原派发冷却、延期和防自环规则保持。

## Autonomous Group channel

Autonomous Group Chat 不创建第二套人物。一次群聊自主 Opportunity 仍然读取同一个 `PersonContextBuilder`：Persona、Mental State、Recall Memory 与真实 Recent Group Events。区别只在 channel policy：

- 这不是 User 刚发来的消息；
- seed 可以沉默并结束整个机会；
- follow-up Character 只有自然想接话时才表达；
- 私聊 Memory 不得因此泄漏到共享房间；
- 本轮不创建 Intent；
- 每个 Character 最多一个 visible action；
- 自主 Group 仍不允许 `GENERATE_IMAGE`。

自主群消息提交后会成为真实 shared durable fact，因此同一 Opportunity 中后判断的成员能看到前一位刚刚公开说的话，后续 Direct/Group/Space 认知也可以正常受到这段真实经历影响。

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

## Experience grounding

Shared reaction prompts distinguish concrete personal history from user accounts,
external material and imagined possibilities. Persona may establish canonical
background and preferences, but an occupation or interest does not establish a
new recent action or result. Concrete first-person experience must refer to
explicit Persona history, actual participated events or sourced observations;
user stories and RSS examples cannot become the character's own experiences.
The same distinction applies to cognitive and future-intent candidates.

This is a model-facing instruction in the existing single reaction call, not a
semantic verifier or a guarantee that all provider output is factual. Runtime
permissions and durable execution receipts remain server-enforced; live review
must separately check unsupported first-person statements and their sources.

Read-only life and execution views expose the recorded opportunity, task, result,
source and linked cognition effects; they do not infer hidden motives or treat an
external assistant's reply as the Person's own observation. Personal Browse/RSS
can retain a sourced observation without changing Mental State or admitting
Memory. The owning observation/admission contract is [Memory](MEMORY.md).

Character continuity acceptance must distinguish a direct source from a retold
account, read from unread material and one person's private experience from
another's. Retrieval tests and activity counts do not establish relationship
quality. Long-horizon Persona and relationship evaluation remains separate from
engineering gates and must allow silence, rejection and uncertainty.

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
- `VOICE_MESSAGE`
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

Sticker 当前是 application/global resource：运行时可以合并内置 pack、全局导入 pack 和 legacy character-local manifest。具体 ownership 见 [`CONVERSATION_RUNTIME.md#stickers`](CONVERSATION_RUNTIME.md#stickers)。

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
- 默认拒绝非用户事件；`direct_autonomous_image_generation_enabled` 开启后，仅显式 `channel=DIRECT` 的持久化 `TIME_TICK / PROACTIVE_INTENT` 可生成，不新增调度器；Group/Space/World/屏幕观察仍拒绝；
- 提示词、动作过滤和执行共享资格策略，过滤 trace 记录拒绝原因；
- 自主图片在提示词编译 LLM 和 Provider 前，使用 SQLite 原子预约：应用共用 UTC 每日默认 3 次，单人物默认冷却 8 小时（均可配置）；单应用进程内最多一个 RUNNING 预约；
- 预约按人物/source event/action index 幂等；失败、中断、过期结果均消耗额度，重启将未知 RUNNING 标记 INTERRUPTED，不自动重试或重复付费；预约表通过 `visual/001-direct-image-generation-jobs` 迁移按需创建；恢复语义限单应用进程，不提供多进程 worker lease；默认关闭不构造预约表；用户消息现有生图不计入自主额度；
- `SELFIE / SCENE` 是合成表达，生成事件写入 `content_scope=SYNTHETIC_CHARACTER_EXPRESSION` 和预约 provenance，不冒充现实拍照；
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

### Intent Admission

与 Memory Candidate 对称，Intent 也要先过服务端准入：

```text
low value? -> skip
exact / near duplicate? -> skip
pending ceiling reached? -> skip
otherwise -> write, with earliest_at >= server floor
```

模型在这一层不是可信输入源：`earliest_hours` 曾经完全没有出现在说明里，于是每个 candidate 都带着字段默认值 0 —— 语义等于“下一次轮询就到期”。准入规则因此写在写入路径上，而不是靠说明文字请求：

- **最小延迟地板**（`proactive_intent_min_delay_minutes`）：`earliest_at = event_time + min(max(earliest_hours, floor), expires_hours)`。地板由服务端给，天花板是 Intent 自己的有效期，所以 clamp 不会把 Intent 排到它已经失效之后。
- **PENDING 上限**（`proactive_max_pending_intents`，0 表示不限）：在 derived transaction 内判定，SQLite 写锁保证两个 reaction 不会各自认为还有空位。
- **近重复丢弃**（`proactive_intent_dedup_enabled` / `proactive_intent_duplicate_similarity` / `proactive_intent_dedup_window_hours`）：先做 `casefold` 全等比较，再比同角色近窗口内所有 Intent 的 embedding。比较集**按 status 不过滤**——已经被压制过的同义 Intent 正是不能再造一条的那个。embedding 失败时仍然写入（Intent 是人物真的想做的事，与 Memory 的处理不同），只把错误记进决策。
- 每个 candidate 的判定（`WRITE` / `SKIP_DUPLICATE` / `SKIP_LOW_VALUE` / `SKIP_PENDING_LIMIT`，含 `requested_earliest_hours` / `effective_earliest_hours` / `clamped` / `duplicate_intent_id` / `similarity`）写进 Runtime Trace 的 `intent_decisions`。

### 谁不能产生 Intent

`PROACTIVE_INTENT`、`SPACE_POST_SEEN`、`SPACE_COMMENT_RECEIVED`、`WORLD_OBSERVATION` 四类事件会被 `_sanitize_channel_actions` 强制清空 `intent_candidates`，并记录为 `DROP_PROACTIVE_SELF_LOOP` / `DROP_CHANNEL_CANNOT_PLAN_INTENT`。

- `PROACTIVE_INTENT` 是自环的那条边：一轮“到期重判”如果又能留下新 Intent，而新 Intent 的 `earliest_at` 落在同一轮或下一轮，就形成永不停止的 LLM 循环。Memory / Mental State 仍照常接受，只切断 Intent 这一条回流边。
- Space / World 被排除是另一回事：它们的 Intent 到期后会走成**私聊**主动消息（`dispatch_proactive_intent` 用该人物最近的会话 ID），也就是“在朋友圈看到评论”变成“私聊找用户说话”，属于跨渠道串味。这与 WORLD_OBSERVATION 只更新认知、不对外表达的既有边界一致。

旧库的 PENDING Intent 在预检和原子领取时还会校验来源：Space、World、Social Post、上一轮 Proactive、瞬时视觉观察，以及明确标为 Group 的来源不能被派发成用户私聊。命中后标为 `SUPPRESSED`，通过现有 Intent audit 记录 `NON_DIRECT_SOURCE`；保留原事件/意图，不调用模型、不占用主动派发冷却。无来源 ID 的旧意图沿用兼容行为，不能仅按文本中的名字猜测接收人。

Direct 用户消息、Wake 和 Proactive 入口明确标记 `channel=DIRECT`，Group reaction 标记 `channel=GROUP`。模型上下文保留近期事件的渠道、会话和可用的评论参与者信息，并声明当前私聊接收对象是用户；角色间交流是背景，不能原样续接为对用户的回复。该上下文约束需要真实模型验收，不等价于文本内容层面的硬保证。已经生成的历史消息保持原状。

### 原 Intent 的有限延期

到期重判仍是原 PersonRuntime 的一次 reaction。可选 `intent_resolution`
只适用于 `PROACTIVE_INTENT`：`EXECUTE` 使用当前合法表达，`ABANDON` 终结，
`DEFER` 配合真实 `defer_hours` 修改原 Intent 的 `earliest_at`，重新置为
`PENDING`。ID、内容、原有效期不变，不创建相同的新 Intent。没有这个字段
的旧输出仍按表达或沉默处理；非 proactive 输出的 resolution 被清除，Trace
记录 `DROP_INTENT_RESOLUTION_WRONG_CHANNEL`。

默认最小延期 15 分钟、单次最多 72 小时、同 Intent 最多 2 次。对应
`proactive_intent_defer_min_minutes` / `proactive_intent_defer_max_hours` /
`proactive_intent_max_deferrals` 是正式 Settings，重启 Character Runtime 后
生效；次数为 0 明确禁止延期。当前配置、已用次数和原 expires_at 随到期
事件传入 Prompt，服务端仍做最终校验。超过原有效期、次数、范围或缺少
延期时间时终结为 `ABANDONED`，留下原因，不猜一个时间。

合法表达优先于矛盾的延期/放弃，记录 `EXPRESSION_CONFLICT`，不会在表达后
重复排期。`EXECUTE` 没有合法表达仍是 `SUPPRESSED`，不伪造执行成功。
合法旧 `ActionType.DEFER` 没有时间，终结并记录
`LEGACY_DEFER_WITHOUT_TIME`；已有历史 `DEFERRED` 行不自动复活。
损坏的可选 resolution 按放弃处理，不因为这个附属字段额外购买修复调用，
也不丢掉同一输出的合法表达。

`core/010-intent-deferral` 只增加 `deferral_count` 和
`intent_deferral_audit`。排期、计数和审计在同一事务提交；时间 ISO/epoch
保持一致，审计包含旧/新时间、决策原因及源 Event ID。模型的结构化
resolution 也进入 Runtime Trace。旧行和原时间保留，旧查询继续使用
`PENDING`，但旧版本再次处理延期时会恢复旧语义，因此二进制降级行为
不能视作已经验收。

调度以条件 UPDATE 原子领取 `PROCESSING`，并在同一事务消耗每人物冷却，
再调用模型；双连接竞争不会分别领取同一 Intent 或绕过正冷却。
延期后的到期只是获得重新判断资格，仍受冷却和等待用户回复限制。
进程中断遗留的 `PROCESSING` 不自动重放模型，需要检查源事件/Trace 后
人工处理；其他独立 PENDING 意图仍由原冷却规则控制。此处不新增循环、
额外意愿识别模型或跨渠道执行权限。

### 派发节奏

到期 Intent 由后台循环派发，并且受每角色冷却约束：

- `proactive_dispatch_enabled` / `proactive_poll_seconds` 控制循环本身；轮询只决定多快发现一个到期 Intent，不改变派发节奏。
- `proactive_min_dispatch_interval_minutes` 是同一角色两次派发之间的最小间隔，**任何派发都会消耗它，包括模型选择沉默的那一轮**——那次 LLM 调用已经花掉了。冷却游标持久化在 `proactive_dispatch_state`，重启不会重置；`has_due()` 也会查它，避免为一个正在冷却的角色白白加载 Runtime。

普通聊天界面不提供手动唤醒按钮或 `/v1/characters/{character_id}/wake` 写接口。当前仍存在 process-local 定时 Character Wake：

- `proactive_wake_enabled`
- 默认约每 60 分钟一次机会
- 用于 direct character，不 wake group chat
- 不是 durable distributed job queue

TIME_TICK Wake 与到期 PROACTIVE_INTENT 都允许人物在自然需要时选择 `VOICE_MESSAGE`；这不是强制语音。语音 action 仍先落成 durable pending message，再复用 Direct 的 VoiceMessageMaterializer 生成音频并更新同一 Event。

Wake/Intent 复用既有文字、语音和 `IMAGE` 表达；图片只能引用当前 Available Images 的合法既有资源，不证明人物刚拍过照片或图中事件已发生。当前不自动获得自主 ImageGen 权限。自主生成图像只发生在用户消息 reaction 中，避免后台无配额地消耗图片 Provider。

Direct Wake/Intent 在原有一次 reaction 中附带有限个人分享观察：从同一人物、机会时间之前的最近 32 条 Event 中，选择最多 4 条有来源的 `WORLD_OBSERVATION`，仅接受 `PERSONAL_BROWSE` / `WORLD_PULSE`。不扫描未读 RSS inventory，不从未保留的 appraisal、失败执行、屏幕记录、Space 私密讨论或其他人物私聊制造经历。每条快照保存源 Event ID、观察时间、来源及原 RSS 阅读范围；正文最多 600 字、来源最多两项各 240 字，Prompt 独立预算 3200 字，并与 Observed Experiences 的检索结果按 ID 去重。候选快照随本次机会 Event 留存，完整原观察仍可按 ID 查验；窗口之外的观察继续使用既有按主题检索，不承诺穷尽历史。

这些材料是人物自身观察的上下文节选，不代表用户在场、已读或已经收到分享。Feed 节选、截断、阅读字数和未抓取原文的说明保持原义。人物可以基于真实共同经历或观察自愿表达，也可以仅因想联系用户说一句或保持沉默；不得臆测用户当前心情、空闲或需要安慰。以上自然表达要求属于原反应调用的模型契约，不是额外语义评估或真实模型质量保证。原 Wake 节奏、Intent 冷却/配额/来源准入继续生效，不增加设置、通知或执行权限。

## 11. Multimodal input is still one Person

普通图片附件、Camera、Screen Share、Voice ASR 都只是进入同一个 PersonRuntime 的不同输入渠道：

```text
text ------------------┐
image attachment ------┤
Camera/Display frames -┤ -> same PersonRuntime
ASR transcript --------┘
```

Visual Capture 的 frame bytes 只作为当前 Vision context，不变成长期人物状态。Event 只保存必要的 capture metadata。

Voice ASR 在浏览器端有 transcript validity gate：空白、纯标点/符号、以及不含汉字的 transcript 都不会创建聊天事实。识别器在非语音上的幻觉形状就是「拉丁字母加句点」（`The.` / `Yeah.`），旧规则「至少两个 ASCII 字母/数字」恰好放行它，所以门槛改为必须含一个汉字（`no_han`）。代价是纯英文、纯假名、纯谚文、纯数字的语音会被拒绝——这是 Chinese-first 语音路径的有意取舍，放开需要同时改门槛与本节。

## 12. Transaction / failure boundary

Source Event 已经是事实后，Derived state 应尽量原子提交。

主 reaction 完成后，一次 derived transaction 可以包括：

- Mental State
- accepted Memory
- Intent
- 0..3 outward Character Messages
- Runtime Trace / ACTION bookkeeping

可选慢工具（例如 ImageGen）应在主事务之外运行，并通过正常 Media/Event 协议追加结果。

Memory 与 Intent 的 embedding 都在这个事务**之外**计算（可能是远端调用，不应占着 SQLite 写锁）；Intent 的 PENDING 配额判定在事务**之内**，靠写锁保证不会超发。

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

## Optional call presentation on the same reaction

Live2D 展示能力由源 Event 的已验证 `metadata.live2d` 决定，仅在当前角色相符时附加可用动作和表情名。Reaction Engine 在同一个正式 LLM 调用的上下文作用域中使用兼容 PersonReaction 的可选 action.live2d schema；未开启时继续原有 schema 和 prompt。提示不拥有业务调度，不新增动作消息，也不触发第二次 LLM 调用。资源白名单与 SSE 展示契约见 [Conversation Runtime](CONVERSATION_RUNTIME.md)，前端生命周期见 [Voice & TTS](VOICE_AND_TTS.md)。

### Channel-specific prompt permission

WORLD_OBSERVATION explicitly declares `actions=[]` and `intent_candidates=[]`.
Its cognition prompt does not include the ordinary chat voice/image/action
instructions. Space reactions likewise receive their permitted public actions,
not generic private-chat media instructions, and cannot schedule private intents.
The server remains authoritative: illegal channel actions are discarded with
`DROP_WRONG_CHANNEL` in runtime trace decisions. This does not disable the
separate existing World Pulse public-comment workflow or expand media rights.

### Observed-experience context projection

The shared PersonContextSnapshot now carries `observed_events` separately from
Memory and Recent Events. Direct reactions can recover matching past personal
observations beyond the recent-event window. Group and public Space callers
request the PUBLIC projection; their shared recent conversation facts are not
mistakenly filtered out by another actor's character ID. World planning reuses
the same bounded experience rendering. Observation recall never schedules a new
browse, purchases a second reaction, or automatically writes Memory.

### Role contact production composition

`build_role_collaboration(access)` binds the recipient's existing PersonContextBuilder, current model, current role/Skill permissions and shared CapabilityExecutor. It supplies the actual shared Group messages or visible Space post and comments, while private memories remain private reasoning context. Contact permits silence or disagreement and at most two recipient decisions plus one separately authorized public research call; these internal calls are attributed to `role_collaboration`, separately from the initiating World's step count. The World continuation composition owns exposing and authorizing the initiating contact tool.

`GitHubIssueHTTPTransport` is an optional operator-owned transport for the separately scoped IssueWriteGrant, not a grant itself. Production POST runs in a disposable HTTP-only child with credentials through stdin, fixed GitHub HTTPS origin, no redirects/retries, a bounded response and a parent-enforced wall timeout including DNS and EOF. Timeout kills/reaps the worker; the durable adapter records UNKNOWN and does not retry an irreversible request. Without a grant the existing adapter returns a draft and never starts HTTP. No UI changes (VISUAL N/A).
