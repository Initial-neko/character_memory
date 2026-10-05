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

Direct Character 如果 reaction 中产生 `GENERATE_IMAGE`，主 reaction 先完成；Visual Runtime 在后台生成并追加 IMAGE Event/SSE。生成产物以 `media_id` 引用 `/v1/media/{id}`；实时投影和 history 投影都使用该引用，人物图库的 `image_id` 仍使用图库 asset 路由。

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

**成本形状**：一条 user message 最多让 `group_max_speakers_per_turn`（默认 5，范围 1..12）位成员各产生一次模型调用；被 @ 的成员一定进入本轮，未点名成员从滚动顺序尾部截断（`deferred_speaker_ids`），因此每轮听到的人仍然轮换。设为 12 即恢复到"每个成员都被问到"的旧行为（对应旧的 12 次模型调用上限）。自主机会另有独立硬上限（`cap = 1..4`）。群成员上限是 `MAX_GROUP_CHARACTERS = 12`，所以满员群里一句用户消息默认最多 5 次模型调用、把 `group_max_speakers_per_turn` 调到 12 时最坏 12 次；这不是 bug，但改动上限或给群加人时要按这个数量级算成本。该项是后端形状旋钮，不在 `HOT_APPLY_FIELDS` 内，因此保存后按 `restart_required` 处理。

**展示粒度**：群聊以 **Turn** 为单位渲染，而不是以持久化 Event 为单位。一个 `PersonReaction` 可以携带多个 action，`_react_member` 按 action 逐条持久化 Group Event（这是有意的，保留），但前端 `web/groups.js` 的 `foldMessages()` 会把 `(turn_id, actor_id)` 相同的连续角色事件折回一个 `ChatTurn`：文本行合并进同一个气泡，表情/图片/语音成为该气泡内部的内容，说话人名字每个 turn 只出现一次。不同角色**永不合并**；同一角色不同 turn 也各自成块，否则会篡改真实的发言顺序。单聊不受此影响，仍然允许更细粒度的连续消息。

`group_character_event` 在每个成员提交后立刻 SSE 推送，不需要等整个群组结束才展示第一条回复。

### Ensemble groups（一键建群）

`POST /v1/ensembles` 只创建一个不可见的 `BUILDING` build record，不提前创建真实 GroupConversation；`POST /v1/ensembles/prepare` 是一键入口，会创建 build record 后执行联网 research。失败时 build 保留为可重试状态，不再因为一次 Provider / structured-output 错误把整次输入和进度删掉。兼容的 `/{build_id}/research` 可继续对已有 build 重试资料整理；单个失败成员还可以通过 `/{build_id}/members/{index}/retry` 局部恢复。

Research 负责群体事实及初始关系；新角色随后复用 `PersonaBuilder.generate()` 按人独立深化对话、分歧、主动和关心方式，失败时回退到 `member_research_to_persona()` 的确定性草稿并标为 `FALLBACK`，不会因某个人的模型输出失败而放弃整批；已存在角色则直接复用现有 Persona，不另行修改。用户可在确认页预览完整草稿并针对单人重新深化（`POST /v1/ensembles/{build_id}/members/{index}/regenerate`），失败成员仍保留原有 `/retry`。`mode=SOURCE` 默认走公开网页资料检索，`mode=ORIGINAL` 则直接以用户原创描述生成人物与关系，不要求联网检索。年龄是弱资料：允许 `18`、`18岁（大学一年级）`、`年龄不详` 或 null；只有能可靠抽出 1..120 的整数时才进入 `PersonaDraft.age`，否则保持 null。角色 identity、description、personality、speech style、relationship 才是建模和后续声线设计的主要输入。

该规则由 `PersonaDraft` 自己的字段校验器执行，所以单角色草稿 `/v1/characters/draft` 走的是同一条弱资料策略：模型对非人类角色写出 `猫龄三岁半` 只会让 `age` 退化为 null，不会连累整份草稿。草稿校验失败后的重试也不再只说“JSON 不符合要求”，而是把目标字段与具体校验错误回传给模型，避免模型原地重发同一份无效 JSON 直到耗尽次数。

建群的公网发现以用户描述本身作为搜索查询，不在查询里附加任何来源词（早先在查询尾部拼 `角色 成员 人物 资料 wiki`，等于用查询词代替搜索引擎的来源排序，并且恰好把本网络无法取回的那类来源顶到最前）。取回按排名逐条推进：前一条打不开（被 SSRF 护栏拒绝、导航超时或 5xx）不会占掉名额，会顺延到下一条候选，因此"前几条恰好都不可达"不再等于整次整理失败。尝试次数有上限，因为每次失败导航都要付一次浏览器超时；排名靠前的候选可取时，仍然只开一批、与旧行为一致。

成员整理采用 partial-success：一位成员格式异常只标为 `FAILED` 并保留 research 原始字段，其他可用成员继续；只要至少 2 位成员是 `READY`，整个 build 就可以进入确认页。前端默认隐藏内部 Pydantic/Provider 细节，用户只看到可理解的“重试这一位 / 重试整理 / 修改描述”。

`/confirm` 由用户勾选后才创建或复用 Character，并在确认成功时创建真实 GroupConversation、把 build re-key 到真实 group id；选中成员的 `relationship_notes` 作为初始公开群关系连同 build 一起持久化并按当前有效成员筛选注入正常/自主群聊上下文，明确标注其不是实际发生过的群消息，不进入 Event/Memory；`/cancel` 放弃未激活 build。confirm **不会自动开聊**——它只建角色、写成员并进入正常群聊生命周期，进群后仍需用户自己发第一句。

READY build 是可跨请求保留的 research 快照，不是提交时的角色注册表快照。再次打开 AI 建群会恢复最近一条 READY / FAILED build；服务重启时遗留的 BUILDING 会转成可重试的 FAILED，而不是永久卡在整理中。confirm 会重新按当前 active Character 匹配每位成员；research 后被删除/归档的旧 ID 不会写进新群，期间新建的同名 active Character 可以直接复用。同一 build 的重复/并发 confirm 只提交一次；成功 re-key 到真实 group id 后，原 build id 仍作为幂等别名接受响应重试。批量容量先用于确认页预测，真正创建每个 Character 时仍在共享 registry lock 下重查硬上限，防止头像/声音 onboarding 释放锁期间有其它创建插入。新角色的 `creation.json.group_id` 在真实 GroupConversation 创建后改写为最终 group id，不保留已经被 re-key 掉的临时 build id。

可选 `use_voice_design=true` 只在用户显式勾选后生效。它要求用户已经手动启动 Qwen3 VoiceDesign sidecar；群聊和 Character 核心 commit 完成后，后台才按角色 identity/personality/speech style 逐个尝试 VoiceDesign + freeze。VoiceDesign 未启动、不可用或单个角色生成失败都只记日志并保留现有默认/回退 voice，绝不回滚 Character 或 Group。

模型调用量级：`SOURCE` 模式一次群体 research + 最多每个新成员一次 `PersonaBuilder` 模型生成（可能附带结构化修复重试）；已有人物不再次生成，失败的单个角色采用本地确定性兜底。`ORIGINAL` 模式不触发 World Observer，但仍需一次群体生成和新人物深化。受角色容量约束：软阈值 10 位、硬上限 20 位，由 API 强制（`api.py` 的 `SOFT_ACTIVE_CHARACTERS` / `MAX_ACTIVE_CHARACTERS`），超过硬上限整批拒绝而不是截断。

### Member failure isolation

Group 当前页面收到的 group-level `reaction_error` 按群保留，不被后续 `idle` 或 history 对账重绘清掉；较新的 `queued` / `typing` 会清除旧提示，迟到的旧轮次错误不会覆盖新轮次。提示是页面内状态，刷新页面不会恢复，不改变 durable message/trace。

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
- 自主机会与用户轮次使用相同的提交 ID 顺序判断新 User fact；客户端 `at` 比旧消息更早，也会打断旧生成。quiet guard 仍按消息时间判断间隔；
- seed 失败时本次机会失败，不换人强行开场；发布 `reaction_error` 后回到 `idle`，正式调度记录 `FAILED`，手动入口返回错误；
- seed 已发言后，某个 follow-up 成员失败只在该成员 decision 中记录错误，其他成员继续判断，且失败不消耗可见消息上限；
- `SUPERSEDED` 只撤销未提交结果；已提交成员消息保留，返回结果和调度记录的 `message_count` 仍计入这些消息；
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

文本、图片和 Visual Capture 的 User 消息入口在同一 SQLite 事务内重查群聊活跃状态并写入事实。若归档已在请求校验后完成，入口返回 404，不写入 User Event、不进入 reaction 调度；本次请求新上传但未被接受的图片也清理 MediaAsset 和文件。消息接收不等待 reaction 的 per-group lock，使新 User 事实仍可及时 supersede 正在计算的 reaction。归档前已经接受的事实继续保留。

### Character Lifecycle：归档 / 删除 / 延迟私聊

Character 生命周期一律是**旁挂标记**，不写进 `persona.yaml`：整个 persona 文档会原样交给模型，UI 状态不能进 prompt。标记文件的存在与否就是 flag，内容里的时间戳只是装饰。

| 与 `persona.yaml` 同级的标记 | 存在表示 | 缺席表示 |
|---|---|---|
| `archived.yaml` | 已归档 | 正常 |
| `direct_pending.yaml` | 私聊尚未开启 | 正常（默认） |

两者极性相反。`direct_pending.yaml` 用"缺席即正常"，因为它是后加的：逐个创建的人物、以及在这个标记出现之前就存在的人物都没有这个文件，必须继续照旧出现在侧边栏。

**归档**：`POST /v1/characters/{id}/archive` 与 `/restore`。隐藏但不删事实；已归档角色仍保留群成员身份，历史照常渲染，也仍可被移出群聊。

**删除**：`POST /v1/characters/{id}/delete`，body `{"confirm_name": "<显示名>"}`。三重校验：

1. 人物必须存在（404）；
2. **必须是已归档状态**（409）——归档是前置的确认动作，否则一份过期的列表就足以删掉用户正在聊天的人；
3. `confirm_name` 必须与该角色显示名完全一致（400）。

用 POST 而不是 `DELETE`：`DELETE` 只能把确认信息放进 body，而 body 恰好是中间层唯一被允许丢弃的部分，丢了会变成"空确认"而不是报错。

删除范围（不可逆）——移除的是**定义**，保留的是**历史**：

- 移除：persona 目录（含 `voice.yaml`）、头像文件、语音引用、在所有群聊里的成员身份、运行时注册；
- 保留：`events`（聊天）、`memories`、`runtime_traces`、`space_posts` / `space_comments`。归档保留头像和人物定义；删除则移除头像和人物定义，只保留聊天、Memory、Trace、空间动态与历史媒体事实。两者的保留范围不能混用。

群成员身份有一条硬约束：`remove_member` 不允许把群降到 2 人以下，而 `GET /v1/groups` 与群归档抽屉都会过滤掉成员数 <2 的群。绕开这条下限不会留下"小群"，而是留下一个用户**既看不见、也无法恢复**的群。所以删除改为**拒绝并点名**那些只有 2 人的群，让用户先归档它。

**延迟私聊（deferred direct chat）**：ensemble 批量建群创建的人物默认带 `direct_pending.yaml`。它们是**完全正常的角色**——照常参与群聊、Space、语音、World——只是暂不进侧边栏，直到用户主动开启。`POST /v1/characters/{id}/open-direct`（幂等）移除标记。

过滤只发生在"会渲染成侧边栏"的那两条路径上：

- `GET /v1/characters`（默认，`include_deferred=false`）；
- `GET /v1/character-profiles` —— 头像管理器会把结果直接回写 `CM.state.characters`，不过滤就会漏回侧边栏。

群成员选择器必须显式请求 `GET /v1/characters?include_deferred=true`。兜底规则：如果过滤会让活跃列表变空，则返回未过滤的列表——浏览器把空列表当成"没有发现任何 Persona"并拒绝启动，"隐藏到空"不是可接受的结果。

`direct_pending` 与 `archived` 会出现在群成员 payload（`group_web` / `group_members_web` 的 `members[]`）上，因为浏览器已经不能再用"不在侧边栏里"来推断"已归档"了。

**归档列表统计**：`GET /v1/characters?archived=true` 为每个角色附带 `activity: {posts, comments, messages}`。三个数字都只数**角色自己的产出**（它发的动态、它写的评论、它说的话），不数对话的另一侧——否则一个从不开口的角色会显得很活跃。仅 `archived=true` 时才计算。

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
纯英文、数字或其组合     -> reject
其它不含汉字的内容       -> reject
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
- Direct 当前页面收到的 `reaction_error` 会保留到较新的用户事实排队或开始处理，不被 idle 重绘或 history 对账清掉；这是页面内提示，不是 durable failure ledger，刷新页面不会恢复它；
- outward action 合法时，辅助 candidate 的小格式错误应局部降级；
- outward action 自己 malformed 时仍需要 repair/failure；
- 一个 group member 失败不应该结束整个 room turn；
- ImageGen 等慢工具失败不应该反向撤销文本回复；
- SSE 丢一个 transient UI event 不应该破坏 durable history；
- Camera/Screen frame bytes 丢失不等于 durable User Event 被删除。

## 14. Scaling boundary

当前 Scheduler/SSE hub 是进程内对象。

SQLite 虽然 durable，但 pending reaction queue、SSE sequence 与 worker state 不跨进程共享。因此当前不要启用多个 Character Runtime app workers 期待自动获得正确 reaction scheduling。

进程重启会丢失尚未完成的 reaction 工作及 transient image/frame payload；启动时没有自动扫描旧 User Event 并重新入队的恢复步骤。已接受的 User Event 仍可通过 history 查询，不能将 durable accept 理解为 restart-safe reaction job。当前 `Last-Event-ID` 重连可以补保留的通知，但不会重新执行丢失的 reaction。

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

Media registration and the event transition form the materializer's commit boundary: if the MediaAsset row cannot be inserted, or the original event disappears before `pending -> ready` commits, the synthesized file and any MediaAsset row are discarded. The canonical text event remains the source of truth.

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

Sticker 属于聊天/社交表达资源。当前 runtime catalog、ownership 与 legacy compatibility contract 收敛在 Conversation Runtime 中；Space 使用同一资源语义，社会层行为见 [SOCIAL_WORLD.md](SOCIAL_WORLD.md)。

Sticker 有**两层 ownership**：

```text
public pool      = application/global resource，所有角色与群聊共享
private pool     = 单个 Character 私有，只有该角色的 runtime 与 Direct picker 可见
legacy compatibility = persona-local manifests，只读合并进 public pool
```

Public pool 是默认层，不是“每个 Character 独占一套资源”。Private pool 是叠加层：一个角色看到的是 public pool **加** 自己的 private pool，看不到别人的。Space 仍只使用 public pool。

运行时仍兼容历史 character-local pack，因此必须区分：

```text
current ownership   = public application resource + per-character private resource
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
load_global_sticker_catalog(...)          # public pool
character_sticker_catalog(global, dir, character_id)   # public + 该角色 private
```

当前 source 描述为：

```text
default+global+legacy                   # public pool
default+global+legacy+character         # 叠加 private pool 之后
```

每个 PersonRuntime 持有自己的 catalog（public + 自己的 private），因此 Direct / Group 的 AI 角色能发自己的私有表情但看不到别人的。多角色场景下 private manifest 只影响拥有它的那个角色。

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

角色 private pool 按 `character_id` 的 sha256 前 24 位分目录（用户可控的 id 不进文件路径）：

```text
<sticker_dir>/characters/<sha256(character_id)[:24]>/manifest.yaml
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

不带参数时返回 public pool：

```json
{
  "scope": "global",
  "source": "default+global+legacy",
  "stickers": []
}
```

带 `character_id` 时返回该角色的 catalog（public + 自己的 private）：

```json
{
  "scope": "character",
  "source": "default+global+legacy+character",
  "stickers": []
}
```

每个 item 带 `"scope": "global" | "character"` 与 `"owner_character_id"`（public 为 `null`）。群聊/用户 picker 不带 `character_id`，只拿 public pool。

#### Global asset

```text
GET /v1/stickers/{sticker_id}/asset
```

只服务 public pool。private id 走这条路由会 404——这是隔离的一部分，不是缺失。

<<<<<<< HEAD
#### PC 可选库删减

```text
DELETE /v1/stickers?sticker_id=<id>
DELETE /v1/stickers?pack_id=<id>
```

PC 表情面板「管理」支持点击单张或「移除整包」，确认后从所有人物、Direct / Group / Space 的**公共**可选库移除；Android 只同步可选目录，不提供管理入口。两种参数必须且只能提供一个，非法 ID 返回400，未知 ID / pack 返回404，重复移除成功且 `removed: 0`。整包按当前合并目录中的 pack_id 移除。管理入口只对 public pack 出现：private pack 属于单个角色，DELETE 没有 scope，在那里提供移除只会得到 404。

移除会在 `<sticker_dir>/removed.json` 原子持久化 ID 列表，与导入共用目录锁，内置、legacy 和导入资源均可移除；重启和重复导入不会复活这些 ID。发送验证、LLM 检索和 prompt 只读 active catalog，已加载 PersonRuntime 立即刷新。历史投影与 asset 读取使用 historical lookup，保留标签、原 manifest 和图片文件，不删除已发送历史，也不回收磁盘空间。需要恢复时，关闭 Core、从 `removed.json` 删除相应 ID 后重启；管理入口当前只做删减。

#### Character-scoped asset route
>>>>>>> origin/main

```text
GET /v1/stickers/{character_id}/{sticker_id}/asset
```

读取该角色的 catalog，public 与 private id 都可用。这条路由也是 Direct/Group SSE 直播消息拼 sticker URL 时的正式路径：直播事件只带 `sticker_id`，不带 scope，只有带上 `character_id` / `actor_id` 才能同时解析两层池子。

### 4. Web ZIP / transparent sheet import

正式 Web import：

```text
POST /v1/stickers/import
Content-Type: application/zip
```

参数：

```text
scope          # global（默认）| character
character_id
filename
auto_tag
```

`scope=global` 时写全局 user library，所有角色与群聊共享。`scope=character` 需要 `character_id`，写入该角色的 private pool；private 素材的 id 带角色哈希前缀 + 内容哈希，因此重导入更新过的图片会得到新 id，老消息引用的旧图仍然可读。scope 非法或缺 `character_id` 返回 400；未知角色 404。

成功后 runtime 会重新加载 catalog，并刷新每一个已加载角色的 Sticker resource。

同一路由也接受 `Content-Type: image/png` 的 3×3 透明九宫图，`filename` 使用 `.png`。当前支持 8-bit、非交错 RGBA PNG，最大 16 MiB / 400 万像素；**长宽不再要求被 3 整除**。分隔线在 1/3、2/3 附近按“近空行/列”探测（alpha < 24 视为透明，忽略生成图常见的微弱 alpha 噪点），并要求分隔线邻居也近空；找不到可信分隔线时报 400，而不是切错。列分隔线按整幅高度投影，行分隔线则在每个列带内单独探测：生成图常把三列的行高错开，整幅宽度上不存在空行，但每个列带各自有干净的空隙；任一列带找不到可信分隔线仍报 400。每格都必须非空并具有透明外边界，布局不明确、损坏、超限或不支持的格式返回400，整张先验证再导入，不会留下部分素材。每格按 alpha 裁边并补2px透明留白，生成同一套装的9个PNG，通过现有 ZIP 导入器原子发布。内容哈希生成固定套装/素材ID，重复导入相同图不会产生重复条目。CLI 的 `archive` 参数同样接受 `.png`；无需新增图像依赖。

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

Public pool。Web import（`scope=global`）和 CLI import 都写到 `sticker_dir`，所有人物/群聊共享。

#### Character private

Web import（`scope=character`）写到该角色的 `characters/<hash>/` 目录。只有该角色的 runtime 与 Direct picker 选中它；群聊里只有该角色作为 actor 时能发自己的 private 表情。private 素材不能通过 global asset route 取到，也不能用别的角色的 id 发出去。

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
Web import  -> global，或 scope=character 时的该角色 private pool
CLI import  -> global
Runtime     -> public pool + 自己的 private pool（+ legacy 只读兼容）
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
- `/v1/stickers` 不带参返回 global scope，带 `character_id` 返回 character scope；
- private pool 的角色隔离：别的角色 id、global asset route、跨角色直接发送都取不到；
- Direct/Group 直播事件用 character-scoped URL 渲染 sticker，private 表情不破图；
- CLI import 仍写 global，`scope` 只属于 Web import；
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
