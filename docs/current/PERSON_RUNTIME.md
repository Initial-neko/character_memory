# Person Runtime

本文描述一个 Character 收到 Event 后，如何形成内部变化与对外行为。

## Read-only capability discovery

`runtime.capability_discovery.available_capabilities(profile, channel=...,
availability=...)` describes the existing WEB_SEARCH / READ_RSS World tools
and eligible Direct CLAUDE_TASK. The caller supplies a trusted per-turn
`CapabilityAvailability` snapshot of configuration, dependency availability,
Direct event eligibility and remaining opportunity allowance. Existing readers
do not acquire a new sidecar permission; Claude and selected Skills still use
the named person's operator-owned `select_role_research` grant. Archived or
invalid people and exhausted opportunities expose no tools. Identity, channel,
budget and grants are absent from model arguments.

Discovery reads local grant/Skill files without initializing providers, calling
a model, reserving budget or persisting an event. Descriptions are not execution
permissions: dispatch must revalidate current grants and use existing executor
checks and receipts. This query does not launch a continuation loop, register
Browser sessions or grant external writes.

DirectResearchService.capabilities(event) now builds the trusted snapshot from
normal Direct USER_MESSAGE eligibility, the configured switch, the named active
profile, runner readiness and durable daily quota/budget. The existing grant()
compatibility view and delegation prompt consume the same descriptions, then
recheck them immediately before dispatch. Missing/closed/uninstalled executors,
revoked grants and exhausted allowance hide new delegation; a model-forced new
request is filtered without an executor call or result interpretation. A completed
receipt for this same character/event/opportunity remains consumable after quota
exhaustion or runner loss, subject to current role/Skill permission and the
executor's original full-payload equality check. The cache hint does not return
a receipt directly, authorize a different task or replay an unknown STARTED job. A fresh budget
snapshot never reserves quota: CapabilityExecutor repeats the shared daily
check inside its existing atomic reservation transaction. Unknown prior usage
continues to consume its conservative reservation. Ordinary chat remains one
normal reaction call.

ClaudePrintRunner.is_available() checks only local command presence and closed
state without starting a process or creating output files. It does not establish
authentication, connectivity, provider compatibility or success; dispatch receipts
and explicit live validation remain authoritative. World/Browser/collaboration
snapshot assembly and general Prompt compilation remain separate integration
work.

WorldActivityService.capabilities(character_id, now=..., rss_candidates=...)
uses the same discovery contract with current active profiles, configured search
service, local candidates and Claude runner/allowance. Composition prepares the
execution schema; listing only reads current facts. New plans cannot execute a
model-selected tool absent from this trusted snapshot. Existing completed World
opportunities retain their original recovery/cached-result paths.

Browser, role-contact and feedback writes require explicit operator sidecar
entries `browser_read`, `role_contact` and `github_issue_write`. A trusted
composition extension descriptor must name its required grant and exact channel;
registration alone never exposes a tool. Existing readers retain their prior
policy. Selected Skills still require one of the explicit Claude grants.

The shared CapabilityExecutor supports at most three execution reservations per
opportunity; existing callers continue requesting one. All begun attempts,
including provider-side denial, consume allowance. New missing-provider calls
are rejected before reservation; completed matching receipts remain readable.
A composition-only CapabilityAdapter can add a bounded tool while retaining
identity, source/channel checks, semantic validation, shared reservation and
receipt persistence. Adapters cannot replace built-ins. Their current permission
validator runs before cache access; provider availability is checked afterward.

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

Preferences, wishes, plans and conditional scenarios retain their original
certainty: wanting to do something does not establish past performance, repeated
habit or a result. Memory is a derived summary, not automatically a verbatim
quote. With no visible original wording the Person paraphrases faithfully and
does not invent attributed quotes, motives or outcomes. Explicitly sourced
completion may still be stated as completed, and visible original wording may
be quoted accurately; the rule must not force denial of known facts or rote
confirmation questions. Current user-provided wording is itself a source for
current quotation and does not require a matching old Memory; confirming a past
quotation is a separate question. Missing details are unknown, not evidence that an action did not happen or
an arrangement was never decided. These boundaries also apply to derived candidates.


The compatible Provider also places the source-fidelity boundary before
character-style instructions in the PersonReaction system message (including
presentation subclasses). Source modality applies to both outward actions and
safe summaries; an explicit request to repeat wording permits no extra characters
or follow-up. Other structured schemas and the supplied Persona/context remain
unchanged. This adds no extra reaction, retry or output-rewriting filter.

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

### Recent experience window

Shared PersonContextBuilder reads exclude known local reaction ACTION receipts
(non-null `trace_id`) before applying the recent-event limit. Explicitly supplied
channel events apply the same filter before truncation. Real
`fact_kind=ROLE_COLLABORATION_MESSAGE` ACTION projections remain experiences,
even if they carry a trace reference. Unknown historical ACTIONs are retained;
the runtime does not guess missing provenance. Character/time boundaries,
chronological ordering and current-event exclusion still apply. Default store
reads retain all receipts for audit; this projection neither deletes facts nor
adds model calls. Direct, Space and World reuse this window; supplied Group
conversation facts keep their existing visibility.

### Observed-experience context projection

The shared PersonContextSnapshot now carries `observed_events` separately from
Memory and Recent Events. Direct reactions can recover matching past personal
observations beyond the recent-event window. Group and public Space callers
request the PUBLIC projection; their shared recent conversation facts are not
mistakenly filtered out by another actor's character ID. World planning reuses
the same bounded experience rendering. Observation recall never schedules a new
browse, purchases a second reaction, or automatically writes Memory.

### Role contact production composition

`build_role_collaboration(access)` binds the recipient's existing PersonContextBuilder, current model, current role/Skill permissions and shared CapabilityExecutor. It supplies the actual shared Group messages or visible Space post and comments, while private memories remain private reasoning context. Contact permits silence or disagreement and at most two recipient decisions plus one separately authorized public research call; these internal calls are attributed to `role_collaboration`, separately from the initiating World's step count. The World continuation composition owns exposing and authorizing the initiating contact tool. The recipient's result interpretation receives the research request identity, executor status and reason separately from the bounded untrusted provider-data text (at most 20,000 characters). With no research receipt, that field is null. Partial data or a provider's own success claim never overrides FAILED, TIMEOUT, DENIED or an unknown/skipped execution. A completed contact means that the shared reply was delivered, not that its nested research succeeded.

`GitHubIssueHTTPTransport` is an optional operator-owned transport for the separately scoped IssueWriteGrant, not a grant itself. Production POST runs in a disposable HTTP-only child with credentials through stdin, fixed GitHub HTTPS origin, no redirects/retries, a bounded response and a parent-enforced wall timeout including DNS and EOF. Timeout kills/reaps the worker; the durable adapter records UNKNOWN and does not retry an irreversible request. Without a grant the existing adapter returns a draft and never starts HTTP. No UI changes (VISUAL N/A).

## Prompt composition ownership

`runtime/prompt_parts.py` provides the pure five-part `PromptParts` value:
Persona, State/Memory, Perception, Capabilities and Runtime Policy. Rendering
only formats snapshots; it never reads permissions, discovers tools, executes
providers, mutates Persona or admits Memory. `compile_context_parts` exposes the
parts for inspection, while `compile_context` remains the text entry point.
Existing channel headers, schemas, Direct recipient ownership, external-data
warnings, Visual no-Memory/no-Intent and autonomous silence rules remain.

The shared reaction compiler optionally consumes
`runtime.research_service.capabilities(event)`. Descriptors use the discovery
owner's `id/summary/arguments_schema/channel/backend/skills` contract; no local
registry, sidecar reader or authorization copy is introduced. Only descriptors
actually supplied for this turn appear, including only their selected Skill
names and hashes. This is an availability description, never an execution grant
or evidence of a completed action. An explicit empty tuple disables discovery;
Direct result interpretation always supplies it, preventing recursive capability
advertising. Services without the discovery method retain their existing
operator-granted Direct suffix. The obsolete global claim that no external tools
exist is replaced by the requirement to describe only actual execution receipts.

The maintained prompt ownership audit is:

| Input builder / fragment | Responsibility and single source | Retention / validation |
| --- | --- | --- |
| `runtime/person_context.py` | Read the existing Person's Persona, state, recalled Memory and observed facts | Retained read-only owner; no second memory or persona lookup |
| `runtime/context.py` | Five-part reaction compilation with existing channel-specific policy | Parts are inspectable; `tests/test_prompt_parts.py` checks capability-only changes and channel boundaries |
| `runtime/reaction_engine.py` | Assemble the same Person snapshot and optional server capability descriptions before one normal reaction | Retained model/resource pipeline; discovery does not add model calls |
| `runtime/person_runtime.py` | Direct delivery, server-side effects and legacy granted task suffix | Retained execution owner, unchanged Persona; Direct task regressions in `tests/test_direct_claude.py` |
| `runtime/direct_research.py` | Restricted external task and one result interpretation | Explicit empty capabilities on interpretation; receipt and no-recursion capture tests |
| `application/group_conversation_service.py` | Group participants, shared history and channel action schema | Retained group suffix; cannot be replaced by generic Direct policy |
| `runtime/social_prompts.py` / World planning, Pulse and appraisal | Pure five-part rendering of existing Person snapshots, public topics and actual source excerpts | Services own scheduling, schemas, grants and execution; no tool or business loop in helpers |
| `runtime/social_prompts.py` / Space post and appraisal | Existing public Person context, actual observations, runtime-derived media limits | Publication remains optional; video style follows current Persona rather than forcing a cute/soft personality |
| World local RSS appraisal | Only the actual local Feed items, not linked-original reading | Pure helper preserves untrusted-data, reading-scope and optional keep=false policy |
| `persona_builder.py` | User-requested character draft creation and product content requirements | Retained unchanged: generating a new draft is a separate product operation, not runtime personality rewriting |
| Visual generation / Live2D presentation | Existing image intent compilation, verified references and per-turn presentation scope | Retained specialized schemas and privacy boundaries; no capability or Persona owner duplication |

Focused prompt capture tests establish deterministic formatting and preservation
of existing boundaries. They do not establish long-term Persona fidelity with a
live provider, real browser visits, or the combined autonomous runtime's behavior;
those require their separate runtime acceptance paths. This backend-only change
has VISUAL N/A because no user interface is changed.

### Provider protocol compatibility boundary

`scripts/validate_capability_provider.py` is an explicit paid, isolated protocol probe, not runtime dispatch. Its pure addition fixture accepts null assistant content and validates each bounded native tool call; malformed JSON, extra/identity arguments and unknown tools fail closed. The production runtime keeps its current typed-JSON decisions and existing executors regardless of probe outcome; no protocol experiment grants a character another capability. The script makes one request per run and does not open the formal database. Live gateway results and cost/latency belong in the PR evidence, not in Persona or fixed activity prompts.

## Bounded World reconsideration

Optional World continuation reuses PersonContextBuilder and the existing Browse
opportunity; it does not introduce a universal Person action schema or new
scheduler. At most three outer World decisions consume actual, scoped executor
receipts as untrusted evidence. STOP may retain a meaningful personal note through
WORLD_OBSERVATION only with matching successful read receipts, preserving the
reading scope. It creates neither Direct actions nor future self Intents, and
does not automatically admit long-term Memory. Interrupted model/provider phases
are never replayed. The source receipt and the character's later interpretation
remain distinct. See [SOCIAL_WORLD.md](SOCIAL_WORLD.md#optional-bounded-world-continuation)
for configuration, local-only recovery and nested collaboration cost boundaries.

World continuation also renders the existing bounded PersonContextSnapshot.recent_events with original channel, participants and shared-fact provenance. This includes real collaboration ACTION projections after restart, without treating them as webpage observations or automatically admitting Memory. Nonparticipants receive no projection.


## Cognition replay boundary

A persisted source is validated against its authoritative fact before reaction
work. Direct/Group claim per-Person execution through the shared conversation
receipt owner, not a second Person loop. Preparation, model-call and committed
cognition are separate durable stages. The model stage is entered before any
reaction call; an interrupted/unknown call is not repeated automatically.
Known pre-call failure permits explicit retry; an explicitly expired preparation
lease can be reclaimed with a new owner token before model entry. Calls already
entered are never reclaimed by time. Superseded work stays terminal. Cached committed cognition is returned without model/tool execution.

The result receipt commits with Mental State, Memory, Intent, outward events and
trace. The replay flag prevents a cached result from publishing new message or
voice work. Source/history remain immutable. Lazy receipt initialization keeps
ordinary Person construction and empty Memory admission free of new DB work.
Optional post-commit effects retain their own lifecycle; committed cognition is
not a claim that an image/voice task finished. Async delivery freezes input/member batches and owns safe startup recovery;
client retry integration remains separate work. See
[Conversation Runtime](CONVERSATION_RUNTIME.md#per-person-cognition-execution-receipts)
for states, legacy reconciliation and repeatable real-process validation.

## Real experience boundary

The existing Person runtime accepts legacy facts without `experience_scope` and
explicit metadata `experience_scope: {schema_version: 1, reality: REAL}` only.
The version must be an integer; extra scope fields, unknown versions, null and
SIMULATED scopes fail closed before real cognition. This guard does not execute
simulations or supply an Activity/branch overlay; Activity lifecycle rows are a
separate owner and any scope they expose is explicitly SIMULATED. See
[ACTIVITY](ACTIVITY.md). Direct and Group check both
the supplied event and authoritative source, then recheck inside the derived
commit transaction. A scope changed after model entry prevents real state,
Memory and visible output commit; the existing receipt retains the failed/unknown
execution, without automatically repeating the provider.

Proactive Intent dispatch is not exempt. The persisted intent is only a pointer;
before dispatch, `intent.source_event_id` is refetched and must still be an
existing real fact. A missing source, a source relabelled SIMULATED, or scope that
cannot be decided suppresses the intent with an auditable reason instead of
producing a real turn. The new `PROACTIVE_INTENT` event carries explicit
`{schema_version: 1, reality: REAL}` scope rather than omitting it as legacy REAL,
and the same source check runs again inside the derived commit transaction, so a
late relabel cannot publish Memory, Intent, Mental State or visible output. A wake
has no intent source; it is an independent real tick and states REAL explicitly.
No prompt, model call or schema change is added by this recheck.

Shared Person context filters recent, observed and recalled experiences to this
boundary. SQL filters precede candidate limits; raw audit queries retain records.
Malformed legacy metadata keeps the existing INVALID-provenance readable-fact
contract; explicit malformed scope in an object does not become legacy REAL.
No historical mental-state repair, new public scope input, schema migration or
Activity execution is implied. Run `scripts/validate_experience_scope.py` for an
isolated actual HTTP/restart check using a deterministic local model.

## Explicit user experience profile

The local operator's explicit tone (NATURAL/CUTE), energy (QUIET/LIVELY) and
verbosity (BRIEF/BALANCED/DETAILED) belong to the experience profile, independently
of Persona, MentalState and Memory. Unset/null fields supply no inferred preference.
No choice is inferred from silence, ignored suggestions or model output.

`GET/PATCH/DELETE /v2/me/experience-profile` reads, changes or clears that profile.
PATCH accepts only `expected_version` and `changes`; DELETE requires an
`expected_version` query parameter. The server chooses the current local operator;
HTTP callers cannot provide an owner, user or character identity. This is the
existing local single-operator deployment, not multi-user authentication or a DSH
grant. Unknown fields and invalid enum/version values return 422; stale revisions
return 409. Reads never load a model or initialize an embedding provider.

The existing SQLite migration ledger owns the head/revision tables. Changes use
one immediate transaction across connections, increment a monotonic version and
record only explicit structured changes. `/versions` returns at most 100 recent
versions. `POST /restore` takes `expected_version` and `target_version`; restoration
creates a new revision rather than moving the version backwards. DELETE removes
old preference snapshots and keeps an empty CLEAR revision; earlier versions then
return 404 on restoration. Existing model traces/backups are not rewritten by
this profile operation; no free-text/private-feedback history is introduced.

PersonContextBuilder reads the current profile on each build without a cache or
read-side schema writes. Missing feature tables mean the legacy empty profile;
explicit API startup/changes own initialization. A corrupt stored profile fails
closed rather than turning arbitrary text into prompt instructions.

Ordinary Direct/Group reactions, Space/World planning, World continuation and
role collaboration receive a separate explicit-preference prompt section. Persona
bytes and role opinions/refusal/silence remain independent. Preferences affect
communication with the user, not permission, external tools, real experiences or
the character's interests. A new build observes corrections, withdrawal, clearing
and restored revisions; an already running model call retains its input snapshot.
With no active choices the prompt section is absent.

### Effective feedback and profile growth versions

Authored EXPLICIT_FEEDBACK text that names a supported dimension also yields a
deterministic preference candidate in `domain/experience_candidates.py`. This is
not a second profile: the interpreter only proposes existing tone/energy/verbosity
fields, records the exact Chinese/English phrase it matched, and never runs a model
or writes on its own. Text with no supported term, or with two equally long
conflicting terms in one field, yields no candidate. `LONG_TERM` versus `SCENE`
comes from explicit time wording; unmarked text is treated as scene-scoped, so an
unstated inference cannot become durable without confirmation.

Explicit choices always outrank inference. A field the operator already chose is
never overwritten: a matching inference shows as satisfied, a differing one shows
as overridden and is excluded from application. `candidate_fields` is the single
place that splits this, so the UI and the apply service cannot disagree.

`GET /v2/me/experience-profile/candidates` lists candidate views without a model
call. `POST .../candidates/{signal_id}/apply` takes a request identity, the current
`expected_profile_version`, and `as_long_term`. Scene candidates require
`as_long_term=true`; applying still uses the ordinary Profile CAS, so a stale
version is 409, and the result is a new normal revision. An already-applied
candidate whose version is no longer active reads as superseded rather than
reusable. `POST .../dismiss` records the operator's decision without changing the
profile. `POST .../revert` re-writes only the fields still holding the inferred
value, back to the candidate's base version as another new revision: history is
never rewritten, and a later explicit choice in those fields is not clobbered.

Decisions live in extra columns of the existing `reply_suggestion_signals` table
(feature migration `experience/005-candidate-decisions`); there is no second table
and no duplicated feedback text. Withdrawing the authored feedback retires its open
candidate; correcting it re-interprets the new text. Clearing the profile expires
every undecided interpretation in the same transaction, so a cleared value cannot
reappear as a fresh suggestion, and the next context carries no preference section.
Old candidate sources never revive after withdrawal or clear.

The 相处偏好 dialog exposes these views behind 反馈建议: status, scope, matched
phrase, your current explicit value, and the value an apply would write. Apply,
revert and dismiss require an explicit click; conflicts preserve the current state
and ask for a refresh instead of retrying. A candidate is never reported as learning
or a changed Persona.

Run `scripts/validate_experience_profile.py` for isolated real HTTP, two-process
CAS, candidate lifecycle, Direct/Group context use and restart/clear checks. It uses
a deterministic provider, so model quality and human experience acceptance remain
separate: the interpreter is a bounded phrase table, not open-ended language
understanding, and no model call is made to interpret feedback.


## User reply suggestions

`application/reply_suggestions.py` helps the local operator draft the next user
message. It does not run a second Character Runtime, submit messages, or update
Persona, Mental State or Memory. `POST /v2/reply-suggestions` accepts only a UUID
`request_id`, an existing typed `context_ref`, and `expected_profile_version`.
Direct follows the current per-character visible history; Group keeps its existing
conversation identity and requires an active conversation with the local `user`
still a member. This does not perform the future Direct conversation migration.

The source must be the latest visible, non-future, REAL message. The bounded
snapshot uses up to 12 visible messages, their existing FactRefs, participant
Persona and explicit experience preferences. It never recalls private memories,
Mental State or runtime traces for user drafts. Message text reuses the public
Direct/Group projection, including `display_text` for user messages. Internal
image instructions and upload filenames are excluded; public attachment IDs may
be referenced, but no image is supplied to this text-only generation, and the
prompt explicitly forbids guessing attachment content.

The shared model adapter produces 3–5 drafts with distinct nonempty intent labels
and texts. Suggestion IDs are server-assigned. Structural distinctness does not
prove different semantic intentions, preference match, naturalness or quality;
these require bounded real-model evaluation and separate human acceptance.
Generation is on explicit request only, uses the existing configured bounded
adapter attempts and records `REPLY_SUGGESTIONS / USER_REPLY_DRAFTS` usage. It
neither adds an automatic cron nor an automatic send/reaction.

`storage/reply_suggestions.py` owns feature migration
`experience/002-reply-suggestions`. A batch claim and its immutable request are
committed before model work, without holding a database transaction across that
work. The same request returns its batch; another payload with that identity is
409. `PENDING` returns 202 and is not replayed after restart or an unknown process
outcome. Failed attempts remain `FAILED`/502, with available rejected provider
text retained for diagnosis; retrying the same identity does not call again.
An explicit new request identity is a new generation, not an assertion that an
unknown former attempt completed. No automatic recovery is inferred from age.

A `READY` batch binds its visible-context digest and profile version. Generation
checks again before publishing; reads recheck current context and membership.
New messages, visible-text/attachment changes, membership changes, archival,
Persona changes, or profile edits/clear invalidate the old batch (409/`STALE`)
and remove its active suggestion payload. `GET /v2/reply-suggestions/{batch_id}`
never initializes/calls a model. Generating or reading a batch never appends a
chat fact. Drafts are not selections or user commitments. The asynchronous Direct/Group
message endpoints accept optional `reply_suggestion={batch_id,suggestion_id?}`
only with a request identity. During the existing acceptance transaction's first
create, the current READY batch and target are checked; the actual persisted
public user text is compared with the stored draft. Matching text is SELECT,
changed text is EDIT; an explicitly associated batch without a suggestion ID is
FREE_INPUT. The client cannot self-report these modes. Unattributed legacy input
is not inferred as a choice. Source fact, signal, durable delivery and acceptance
receipt commit or roll back together. A replay returns the original receipt
before revalidating a batch made stale by that very send. When attribution is
absent, its field is excluded from the acceptance fingerprint to preserve older
receipts. Draft clicks alone create none of these signals.

`storage/reply_suggestion_signals.py` owns migration
`experience/003-suggestion-signals`. Sent text remains in the existing fact;
signals reference it by FactRef and do not duplicate its body. Explicit signal
reads at `GET /v2/reply-suggestions/{batch_id}/signals` do not call a model and can
show the operator's recorded feedback after the draft batch has become stale.
Generation identifies the local USER as the draft author and uses a user-draft preference lens instead of the companion-reply instructions. Persona describes the companion, not undocumented user identity; messages, summaries and memory candidates must keep unspecified user gender/identity neutral. Generation supplies the current SuggestionOutput JSON Schema and its exact suggestions top-level field to the model before the untrusted visible-conversation material; schema validation remains strict and failed outputs are not renamed into valid batches.

`POST /v2/reply-suggestions/feedback` records only explicit IGNORE or
EXPLICIT_FEEDBACK, with its own durable request identity. Free text is retained
as authored feedback without inferring personality, mood or long-term choices.
Optional explicit enum `preference_changes` requires `expected_profile_version`;
feedback and the existing Profile CAS update commit atomically, changing the next
context. Retrying the same feedback returns its original recorded outcome even
if the batch is now stale, without applying preferences twice. A different
payload with that identity conflicts. Ignoring does not change preferences.
Signals store applied version references, not restorable preference snapshots.
Clearing Profile removes its choices/history as above; it neither deletes
independent authored feedback/source facts nor replays them into preferences.

Signal author governance uses feature migration `experience/004-signal-governance`.
Existing signals start at revision 1. A strict UUID request identity and
`expected_revision` protect PATCH `/v2/reply-suggestions/feedback/{signal_id}`
(authored EXPLICIT_FEEDBACK text only) and POST at the same path plus `/withdraw`
(any operator-owned signal). The signal change and its metadata-only idempotency
receipt commit together; stale versions/different payload reuse conflict (409),
unknown/other-owner signals are not found (404), and invalid fields are 422.
Withdrawn signals cannot be revived by a new correction. A successful mutation
returns only its identity, operation and applied revision, never an old text copy.
Replaying it after a later change returns that metadata without restoring text.

Correction replaces the current authored note; withdrawal clears it and marks the
signal withdrawn. Audit receipts retain hashes and version metadata rather than
prior plaintext snapshots. This is a logical projection change, not secure erasure
of SQLite history pages or external backups. Signal reads expose revision and
withdrawal state; `?active_only=true` filters withdrawn records. The historical
kind, source FactRef and accepted message remain unchanged. Correcting or
withdrawing a signal does not silently undo a separately applied Profile choice;
current preferences remain governed by their explicit CAS/clear/restore API.
The original feedback POST replay returns the current governance view, and a
message-acceptance replay refreshes its signal view after verifying the same
source identity. Neither route revives an old note or reports a withdrawn signal
as currently active; neither creates another send or signal.

This boundary does not implement Activity, automatic interpretation or periodic
learning from free text. The frontend draft and feedback controls below use these
APIs; real-model semantics and final user quality acceptance remain separate.


### Explicit preference Web panel

The chat topbar's 相处偏好 entry opens a native modal dialog over the existing
conversation. `web/experience_profile.js/css` owns this UI; it calls only the
local operator Profile API, never a model or a Persona mutation. Choosing a radio
option edits a local draft; only explicit save submits changed fields with the
read version. Unspecified sends null to withdraw that dimension. All characters
share these explicit choices; the panel does not configure proactive notifications.

Loading failures leave chat usable. Version conflict or unknown save outcome
preserves the draft and blocks another mutation until an explicit latest read;
the previous draft summary remains visible for manual reselection. Late requests
cannot overwrite a later opening. A closed panel does not cancel an already
submitted write; reopening reads the actual server state. Versions restore only
after confirmation as a new revision. Clear confirmation deletes current choices
and old snapshots, rather than offering recovery of deleted preferences. Both
operations use the same CAS boundary; in-flight model turns keep their snapshot.
The dialog supports Escape/focus return and internal scrolling on narrow screens.
The Profile panel remains distinct from the chat reply-suggestion and feedback UI.


### User-controlled reply drafts and authored feedback

The ordinary Direct/Group composer offers an explicit 帮我想想 action. Page load,
conversation changes and message arrival never trigger generation. It uses the
latest public message FactRef and current Profile version. A 202 result offers
an explicit GET check; unknown POST results can retry the same generation request
identity. Known failures require an explicit new generation, never an automatic
paid loop. Suggestion buttons only fill an editable draft, confirming replacement
of existing text. They never submit chat or create SELECT signals.

Only the ordinary composer submit adds its selected suggestion/batch attribution
to existing text sends. W2 persists it with the immutable request payload; the
client consumes the draft association after durable local recording, and retry
continues that original identity. Accepted public text determines SELECT/EDIT or
explicitly associated FREE_INPUT on the server. New context, Profile or target
invalidates choices while leaving typed text. Calls, visual sends and life-reference
messages keep their existing paths and do not acquire ordinary-text attribution.

Explicit 不用这些 records IGNORE without inferring a preference. 建议反馈 reads the
last batch's authored notes; users can submit, correct and withdraw them with the
existing UUID/revision contracts. Text is recorded without automatic semantic
interpretation. Optional explicit tone/energy/verbosity choices in the same dialog
apply only selected fields; KEEP sends no field, and withdrawal sends null for
that dimension. A blank note is allowed with an explicit change. These choices
use the current Profile version, atomically with the authored feedback, and affect
the next context. Conflicts preserve text and choices without overwriting another
revision; an explicit latest read and confirmation are required before submitting
on a new version. Viewing or restoring preferences reuses the separate Profile panel.

An unknown feedback write keeps its exact request identity and body for retry.
A historical applied version is not presented as currently effective after a later
clear, restore or edit. After actual accepted SELECT/EDIT/FREE_INPUT, an authored
note and explicit Profile choices remain allowed for that now-stale batch. This
exception requires a local-operator-owned batch and server-owned accepted-source
signal, not a click or client assertion. It neither reads an archived/left group's
context nor revives suggestions; new choices, generation and IGNORE still require
current READY validation. Source profile_version stays historical while
applied_profile_version identifies the actual current-CAS result. Replays return
the original outcome without reapplying choices, even after clear or restore.
Historical source facts stay unchanged when feedback is corrected or withdrawn;
text correction/withdrawal does not undo independently saved Profile choices.
## Operator-confirmed long-term Person goals

Goals are durable per-`character_id` metadata in the same Core SQLite database,
separate from Intent (the next action), Persona and Memory. No new scheduler,
model call, automatic Intent dispatch or prompt/context change is implied.
Current APIs are local operator management, not an external Agent grant.

`POST/GET /v1/characters/{character_id}/goals`, `GET /{goal_id}` and
`POST /{goal_id}/actions` create/read or advance a bounded ordered goal.
Creation validates a known Person without initializing the model. Historical
reads use the durable Person association; a different Person or missing goal
returns 404. Strict request fields reject malformed input with 422; state,
version, evidence and request-identity conflicts return 409.

Creation is ACTIVE at stage zero/version one. PAUSE and RESUME switch ACTIVE
and PAUSED; only ACTIVE can COMPLETE_STAGE. A non-final acceptance advances one
stage, and the final acceptance makes the goal COMPLETED. ARCHIVE preserves
history and cannot be undone by a new request or old retry. Editing/reopening,
shared goals, autonomous completion and growth-version rollback remain separate.

Each mutation has a bounded `request_id` and expected version (creation has no
expected version). One immediate transaction owns source revalidation, stage
acceptance, state CAS and mutation receipt. Two connections cannot accept the
same version twice. Reusing the identity with different payload/goal conflicts;
a legitimate retry returns its original acknowledgement plus the current goal,
not an old active state that revives an archived goal. Reads use one deferred
SQLite snapshot for state, stages and source statuses, preserving any caller-owned
transaction without commit/rollback. They do not migrate or write state; schema
initialization is an explicit composition action.

COMPLETE_STAGE requires `confirmation=OPERATOR_ACCEPTED` and 1-8 distinct
FactRefs. EVENT evidence belongs to this Person; CONVERSATION_EVENT evidence
must be authored by the Person or have its actual runtime witness trace, not
merely current group membership. Both must currently pass the existing REAL
experience boundary. The same checks run inside the commit transaction; missing,
foreign, simulated or malformed sources return only `EVIDENCE_UNAVAILABLE`.
Legacy invalid-metadata markers are not accepted as real Goal evidence; the
existing Memory provenance audit behavior is unchanged.
Operational database errors remain errors, not a fabricated missing source.

Stage acceptances store references, source hashes, optional bounded operator
notes and actual UTC acceptance time, never copied source text or new facts.
GET resolves current evidence as CURRENT, CHANGED or UNAVAILABLE without returning
source bodies. Source deletion, scope changes or loss of witness trace must not
leak cached text. Historical completion means the operator accepted that stage;
a source containing a claim is not proof of real-world completion, and the model
cannot auto-accept it. Changed/unavailable evidence is visible separately and
does not silently rewrite that historical acceptance or its source facts.
