# Activity 生命周期、关系练习、分支复盘与共同记忆 (P10 + P11 + P12)

状态：**IMPLEMENTED — LIFECYCLE + RELATIONSHIP PRACTICE + BRANCH/REVIEW/ADOPTION (P10–P12)**。本文冻结 Activity 内核、P11 的关系练习执行切片，以及 P12 的分支、后果、结束、复盘与显式采用。分支只在模拟情境内存在；只有用户显式采用一条已完成分支后，系统才写入**一条** REAL 事件和**一条** REAL 共同记忆。整个会话（情境、主记录、分支、复盘、采用回执）都在同一个 SQLite 中保存，读取与重启恢复不调用模型。

Activity 是 `Activity → Participants → State → Actions → Consequences → Events → Reflection` 责任链的所有者。当前只有一个持久生命周期；只有 P11 的一次显式练习回合会调用模型，没有调度器或自动推进。

## 1. 状态机

```
CREATED --RESUME--> ACTIVE --PAUSE--> PAUSED --RESUME--> ACTIVE
   |                  |                |
   +------END---------+------END-------+--> ENDED (terminal)
```

| 起始 | 允许操作 | 目标 |
| --- | --- | --- |
| CREATED | RESUME / END | ACTIVE / ENDED |
| ACTIVE | PAUSE / END | PAUSED / ENDED |
| PAUSED | RESUME / END | ACTIVE / ENDED |
| ENDED | 无 | — |

- 创建**不**自动激活。`CREATED` 是一个可读取、可继续的已保存状态；把它隐式当作 ACTIVE 会让「保存」和「开始」失去区分。
- `ENDED` 是终态。结束后再 PAUSE/RESUME/END 一律 `409 ACTIVITY_STATE_CONFLICT`，而不是静默 no-op。
- 状态机本身是纯函数（`domain/activity.py:transition_target`），持久化层只负责把非法转移映射成 409。
- P11 关系练习完全复用这张状态机：创建返回 `CREATED`，前端进入练习时显式 `RESUME`；暂停后不允许产生新的模拟后果，`ENDED` 后只读。

## 2. 参与者与身份边界

| `actor_type` | 身份字段 | 是否正式人物 |
| --- | --- | --- |
| `CHARACTER` | `character_id` | 是。必须已存在于 Persona 注册表；复用同一个 Person，不复制 Persona、Mental State 或 Memory。 |
| `SIMULATED_CHARACTER` | `local_id` + `display_name` | 否。身份只存在于该 Activity 内，持久键为 `sim:<local_id>`。 |
| `DSH` | `subject_id` | 否。外部 DSH 主体，必须先在 DSH 管理面注册并由操作者写入参与者列表；持久键为 `dsh:<subject_id>`，`display_name` 由服务端从注册主体解析。见 [DSH_AUTHORIZATION](DSH_AUTHORIZATION.md)。 |

正式参与者的 `display_name` 就是 `character_id`：存储层不读 Persona 注册表，显示名的解析属于读取方。`local_id` 不得以 `sim:` 开头被当作正式 ID 使用，正式 `character_id` 也不得带 `sim:` 前缀——两侧都拒绝，前缀不会成为模糊地带。

- 模拟人物**不得**创建第二个 Person：不写 `personas/`、不进入 `/v1/characters`、不注册 runtime、不产生 `character_id`。持久化层保存 `character_id=NULL` 与 `local_id`，`participant_key` 的 `sim:` 前缀使两者在数据库里也不可能混淆。
- 一个 Activity 至少要有一位正式人物参与。纯模拟阵容没有可挂载后续结果的真实主体，创建返回 422。
- `local_id` 不得与同一 Activity 内的任何正式 `character_id` 相同；参与者键必须唯一。
- SIMULATED 身份在同组活动中可以复用同一个 `local_id`，但跨 Activity 不是同一个实体；P12 若需要跨活动延续，必须显式建模，不能靠名字推断。

## 3. 版本与冲突

- 每个 Activity 有单调递增的 `version`，创建时为 1。
- 每个生命周期操作携带必填 `expected_version`；版本不匹配返回 `409 ACTIVITY_VERSION_CONFLICT`，**不**静默覆盖。
- 状态变更使用 `UPDATE ... WHERE id=? AND version=? AND status=?`，因此两个连接以相同版本并发操作时只有一个成功，另一个得到 409。
- 版本上限 `MAX_ACTIVITY_VERSION = 2**31-1`；耗尽返回 `409 ACTIVITY_VERSION_EXHAUSTED`，不回绕。

## 4. 幂等

- `request_id` 是调用方生成、长度 8–80 的 `[A-Za-z0-9_-]` 字符串。创建与三类生命周期操作共用**一张全局回执表**。
- 相同 `request_id` + 相同请求内容返回原结果并标记 `replayed: true`，不再次执行，也不再次递增版本；回执与状态在同一条 immediate 事务内提交，重启后仍可重放。
- 相同 `request_id` 但内容不同（不同操作、不同 Activity、不同 `expected_version`）返回 `409 REQUEST_ID_CONFLICT`，避免一个请求 ID 被重新绑定到别的目标。

列表排序使用持久化的单调 `updated_seq`，不使用墙钟时间戳：系统时钟粒度可能让两次创建落在同一微秒，排序就会退化成随机 uuid。`updated_seq` 由存储层在写事务内分配，跨进程、跨重启保持同一顺序。

P11 的练习回合另有一张按 `(activity_id, request_id)` 唯一的回合表。相同请求 ID + 相同内容重放已完成的回合，不再次调用模型；内容不同返回 `409 REQUEST_ID_CONFLICT`。回合表与 Activity 回执分开，因为「发送一句话」不是生命周期迁移，不能递增 Activity 版本。

## 5. 读取与恢复

- `GET /v2/activities`、`GET /v2/activities/{activity_id}` 只读本地 SQLite；不加载模型 bundle、不初始化 embedding provider、不写任何行。
- 进程重启后直接读取即可继续：状态、参与者、版本与回执都在 SQLite。恢复一个已存在 Activity 不需要模型调用；验收用真实 HTTP 双进程证明调用数为 0。
- 读取不是执行：本切片没有「继续活动的下一步」，所以读取/恢复既不推进状态也不生成事实。

## 6. 与 experience scope 的关系

- Activity 产出的事实一律是 **SIMULATED**。响应中的 `experience_scope` 由服务端生成，调用方不能提交：
  `{"schema_version": 1, "reality": "SIMULATED", "activity_id": "<id>"}`。
- 该 scope 被现有 `domain/experience_scope.py` 的真实边界拒绝，因此活动事实不会进入 Direct/Group 的真实认知、真实 Memory 召回或关系认知。
- P12 的分支事件由服务端生成的 `branch_id` 写入：`{"schema_version": 1, "reality": "SIMULATED", "activity_id": "<id>", "branch_id": "<branch-id>"}`。主记录事件不带 `branch_id`。调用方不能提交或选择这两个字段。
- 读取层逐行校验 scope 必须与行所属的 `(activity_id, branch_id)` **完全相等**；缺字段、多字段或串到另一分支都按 `503 SCENARIO_CORRUPT` 处理。
- P10 不写任何事实；P11/P12 的主记录和分支只写自己的 `scenario_events` 行，每行都带冻结 scope。未采用的分支仍不写 Event、Memory 或 Direct/Group conversation fact。
- P13 的 DSH turn 写入独立的 `activity_facts` 行（`(activity_id, seq)` 为身份），每行同样带精确的三字段 SIMULATED scope 和不可变的 `authorization_json`。它不写 `scenario_events`、Event、Memory 或 Direct/Group conversation fact；撤销后事实保留并由操作者投影标注 `REVOKED_AFTER_COMMIT`。
- P12 唯一允许写 REAL 行的路径是显式采用（§8.6）：一条 REAL `PRACTICE_ADOPTED` 事件加一条 REAL `SHARED` 记忆，二者在同一事务中与采用回执一起提交。

## 7. 关系练习执行切片 (P11)

### 7.1 情境与阵容

- 一个关系练习 = 一条 Activity + 一条 `relationship_scenarios` 场景行 + 参与者描述。创建在一条 immediate 事务里提交；场景写失败不会留下孤立 Activity。创建请求的 `request_id` 重用 Activity 回执，重放返回原场景。
- 阵容固定为 **2–3 位**：至少一位现有正式人物（`CHARACTER`），至少一位活动内模拟人物（`SIMULATED_CHARACTER`）。正式人物必须已存在于 Persona 注册表；模拟人物只写 `local_id`、`display_name` 和可选描述，不注册 Person、Persona 或 runtime。
- 描述只允许指向本次阵容中的参与者键；未知键在契约层拒绝。创建时返回 `CREATED`，不隐式开始。

### 7.2 一次练习回合

`POST /v2/scenarios/{activity_id}/messages` 是唯一会调用模型的练习路径，且只接受 `ACTIVE` 场景：

1. 在 immediate 事务中领取 `(activity_id, request_id)` 回合，写入用户消息事件（SIMULATED scope），并在同一事务中绑定可选的建议归因。
2. 读取只读场景快照和既有正式人物 Persona，构造一次结构化模型调用。提示要求：每位伙伴给出自己的看法、观点可辨认、允许分歧、不统一成一套标准答案、不说教；只有模拟人物按身份回应用户；不编造用户没说过的事实，不假装练习已产生现实后果。
3. 校验输出：`perspectives` 必须恰好覆盖本次阵容；`stance` 与 `perspective` 在归一化后必须两两不同；`replies` 只能来自模拟人物。任何一项不满足都让回合失败，而不是发布一个看似合理但并非模型产出的记录。
4. 写入全部 `PERSPECTIVE` / `REPLY` 事件和 READY 状态；所有事件共享同一次领取的 `turn_id` 与冻结 scope。

正式人物只提供视角，不代替模拟人物说话；模拟人物可以拒绝、犹豫、误解、提出条件或只回应一部分，不要求达成一致。

### 7.3 失败是显式且持久的

- 回合状态为 `PENDING → READY | FAILED`。模型、schema、身份校验或写入冲突失败时，回合持久标记 `FAILED`；用户消息保留，伙伴事实一条都不写。
- 相同 `request_id` 重放失败回合返回相同 `502 SCENARIO_GENERATION_FAILED`，绝不自动重试模型。需要一个新回合时必须使用新的请求 ID。
- 生成期间 Activity 被暂停/结束时，提交前会重新检查状态；未提交的伙伴内容不写入，返回 `409 SCENARIO_STATE_CHANGED`，回合记为失败。
- 中途崩溃留下 `PENDING` 时，重放返回 `409 SCENARIO_ROUND_PENDING`；用户可显式 `abandon` 把它标记为未完成，系统不补写任何伙伴内容。
- 读取、暂停/继续、列表和重启恢复都不加载模型；真实 HTTP 验收用 JSONL 调用日志证明读取为 0 次调用。

### 7.4 隔离与建议复用

- 练习事实只存在 `scenario_events` / `scenario_rounds` / `relationship_scenarios` / `scenario_participant_notes`。每行事件都持久化 `{"schema_version":1,"reality":"SIMULATED","activity_id":...}`；读取层再次校验，缺少 SIMULATED 或 `activity_id` 即 `503`，不会把异常行当真实事实返回。
- 结构上不写 `events`、`memories` 或 `conversation_events`，因此 Direct/Group 的真实召回、关系认知和 Memory 管理看不到练习内容；真实 Direct 的建议上下文也看不到练习文本。反向同样成立：练习提示不读取真实私聊事实。
- 用户草稿完全复用既有建议链路：`Reference` 增加 `SCENARIO_EVENT` 种类（必须带 `activity_id`，且与 `conversation_id` 互斥）。服务端从练习快照构造只读上下文，继续使用 `PENDING/READY/FAILED/STALE` 批次、画像版本 CAS 和现有归因表。
- 选择/编辑/自由输入仍由既有 `SELECT/EDIT/FREE_INPUT` 分类器在用户消息被接受后记录，来源引用是 `SCENARIO_EVENT`；没有第二套输入方式或第二套建议生成器。

### 7.5 UI

- 侧边栏入口「关系练习」打开抽屉：已有练习列表 + 创建表单（标题、情境、想练什么、顾虑、1–2 位正式人物、1–2 位模拟人物及描述）。
- 进入练习后共用一个聊天外壳：情境卡、每位伙伴一张观点卡（立场 + 看法）、模拟人物的回应气泡；输入框、建议草稿、编辑与自由输入与 Direct 完全共用。顶部明确显示「模拟练习 · 不写入现实记忆」。
- 暂停/结束场景禁用发送；失败回合在记录中显示「这一轮没有完成」，不显示任何伪造的伙伴内容。
- 桌面/移动截图和视觉验收证据属于前端验收；参考图与人工签收仍按 Issue 门禁执行，不能用 DOM/Playwright PASS 代替。

## 8. 分支、后果、结束、复盘与共同记忆 (P12)

### 8.1 分支点

- `POST /v2/scenarios/{activity_id}/branches` 只在 Activity 为 `ACTIVE` 时接受 `{request_id, after_event_id, message}`。
- `after_event_id` 必须是该 Activity **主记录**里已经存在的事件；分支事件、其他 Activity 的事件和未知 id 一律 `409 SCENARIO_BRANCH_POINT_INVALID`。`after_event_id` 是分支点：模型只看到主记录截至该事件（含）的上下文，之后的主记录不再进入这条分支。
- `message` 是用户“换一种说法”的替代回合。服务端生成 `branch_id` 与独立 `turn_id`；分支的 `USER_MESSAGE / PERSPECTIVE / REPLY` 全部写 `scenario_events.branch_id`，并带 §6 定义的带 `branch_id` 的 SIMULATED scope。
- 一条 `READY` 分支还必须带模型生成的 `consequence`。它只描述这条想象路径可能导向什么，不得写成现实已发生的结果，也不得替用户决定。

### 8.2 分支之间、分支与主记录的读取隔离

- 主记录读取（`GET /v2/scenarios/{id}`、建议上下文、复盘主记录）只读 `branch_id IS NULL`。分支读取（`GET /v2/scenarios/{id}/branches/{branch_id}`）只读该 `branch_id`。
- 分支不会移动主记录的 `latest_event_id`，不进入建议批次，不改变 Practice 回合表，也不会让主记录多出任何事件。
- 同一分支点可以创建多条分支；它们各自有独立 `turn_id`，读取互不污染。主记录与所有分支共享同一个 Activity 的参与者阵容和 SIMULATED 边界。
- 分支事件仍只写 `scenario_events`；`events`、`memories`、`conversation_events` 在采用前保持零写入。

### 8.3 分支失败与重试

- 分支状态为 `PENDING → READY | FAILED`。模型、schema、身份校验或状态变化失败时，分支持久标记 `FAILED`，只保留用户的替代回合，伙伴事实一条都不写。
- 相同 `request_id` 重放失败分支返回相同的 `502 SCENARIO_BRANCH_GENERATION_FAILED`，绝不自动重试模型。
- `POST /v2/scenarios/{id}/branches/{branch_id}/retry` 只接受 `FAILED` 分支和一个**新的** `request_id`；它复用原来的替代回合，只重新生成伙伴内容与后果。`PENDING` 返回 `409 SCENARIO_BRANCH_PENDING`，`READY` 直接读取既有结果。
- 生成期间 Activity 被暂停/结束时，提交前重新检查状态；未提交的伙伴内容不写入，返回 `409 SCENARIO_STATE_CHANGED`，分支记为失败。

### 8.4 结束与复盘状态转移

```
CREATED / ACTIVE / PAUSED --END--> ENDED (terminal)
ENDED --POST review--> review PENDING --model--> READY | FAILED
review FAILED --new request_id--> review PENDING --model--> READY | FAILED
review READY --any request_id--> READY (replayed, no model call)
```

- 结束仍走 P10 的 `END`，只把 Activity 变成终态；它不自动调用模型。结束后不再接受练习回合或新分支。
- 复盘要求 Activity 为 `ENDED`，否则 `409 SCENARIO_NOT_ENDED`。复盘不改变 Activity 的状态或版本。
- **一个 Activity 只有一条复盘结果**。每次尝试另有一条按 `(activity_id, request_id)` 唯一的尝试回执；`READY` 结果写入后，新的请求 ID 只读取既有复盘，不重复写入、不再调用模型。
- 失败是显式且可恢复的：失败尝试持久 `FAILED`，不伪造 `overview/strategy/feedback`；相同请求 ID 返回相同失败，新的请求 ID 才能重试。并发尝试返回 `409 SCENARIO_REVIEW_PENDING`。
- 复盘只根据主记录和已完成分支的 `consequence` 总结，明确区分“实际练习”和“想象分支”，不把分支写成现实后果。失败后重试成功只产生一条复盘记录。

### 8.5 采用分支：唯一进入真实上下文的路径

- `POST /v2/scenarios/{activity_id}/adopt` 接收 `{request_id, branch_id, character_id?}`，要求：
  1. Activity 已 `ENDED`（否则 `409 SCENARIO_NOT_ENDED`）；
  2. 复盘 `READY`（否则 `409 SCENARIO_REVIEW_NOT_READY`）；
  3. 目标分支 `READY`（否则 `409 SCENARIO_BRANCH_NOT_READY`）；
  4. `character_id` 是该 Activity 的正式参与者；只有一位正式人物时可省略，两位时必须显式指定（缺失/未知分别 `409 SCENARIO_ADOPT_CHARACTER_REQUIRED` / `SCENARIO_ADOPT_CHARACTER_INVALID`）。
- **每个 Activity 最多采用一条分支**。同一分支的再次采用返回原结果（`replayed: true`），不新增事件或记忆；采用后再采用另一条分支返回 `409 SCENARIO_ALREADY_ADOPTED`。
- 采用在同一 immediate 事务内完成三件事：写一条 REAL Event、写一条 REAL Memory、写采用回执。任何一步失败都整体回滚，不存在“有回执没有记忆”或“有记忆没有来源”的中间态。
- REAL Event 固定为 `event_type=PRACTICE_ADOPTED`，metadata 带：
  - `experience_scope: {"schema_version":1,"reality":"REAL"}`；
  - `channel: "PRACTICE"`；
  - `source: {kind:"SCENARIO_ADOPTION", action:"ADOPT_BRANCH", activity_id, branch_id, adoption_request_id, review_request_id}`；
  - `practice: {title, overview, strategy, feedback, next_step, consequence, branch_after_event_id}`。
- REAL Memory 固定 `memory_type="SHARED"`、`source_event_id` 指向该事件，metadata 带同一来源。Memory 内容明确说明“这是模拟练习后由用户显式采用，不是现实里已经发生的真实事件”。嵌入是辅助步骤：provider 失败时仍写记忆，但绝不把未采用的分支写进去。
- 未采用的分支、复盘行、主记录和所有 `scenario_events` 仍保持 SIMULATED，真实 Direct/Group 认知、Memory 召回和关系认知都因 scope 边界而看不到它们。

### 8.6 共同记忆的可引用内容

采用后，下一次真实聊天通过既有 `PersonContextBuilder` 读取到该 REAL `PRACTICE_ADOPTED` 事件，并可通过 `SHARED` 记忆召回。记录中可引用的只有：

- 练习过什么（`overview` / 情境标题）；
- 用户实际用了什么策略（`strategy`）；
- 收到什么反馈（`feedback`，保留分歧与拒绝，不美化）；
- 可选的下一次尝试（`next_step`）与这条分支的想象后果（`consequence`）。

记录中同时保留“模拟练习、用户显式采用、不是现实事件”的限定语，防止人物把练习说成真实经历。采用前，任何聊天上下文都不包含这些内容。

### 8.7 保存与恢复

- 情境、主记录、回合、分支、分支尝试、复盘、复盘尝试和采用回执都持久化在 SQLite。
- 重启后直接读取即可继续查看分支/复盘/采用状态；读取、`GET /review`、分支/复盘的重放都不加载模型。用真实 HTTP 双进程验收证明调用数为 0。
- 会话没有“自动下一步”：不调度、不自动结束、不自动复盘、不自动采用。

## 9. HTTP 表面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v2/activities` | 创建，`201`；未知正式人物或纯模拟阵容 `422`；`request_id` 冲突 `409` |
| GET | `/v2/activities?limit=&status=` | 列表，最近更新优先，`limit` 1–100 |
| GET | `/v2/activities/{activity_id}` | 读取；不存在 `404 ACTIVITY_NOT_FOUND` |
| POST | `/v2/activities/{activity_id}/pause` | `{request_id, expected_version}` |
| POST | `/v2/activities/{activity_id}/resume` | 同上；`CREATED` 与 `PAUSED` 均可 |
| POST | `/v2/activities/{activity_id}/end` | 同上 |
| POST | `/v2/scenarios` | 创建关系练习，`201`；阵容/描述校验 `422`；重放返回原场景 |
| GET | `/v2/scenarios?limit=&status=` | 练习列表，最近更新优先 |
| GET | `/v2/scenarios/{activity_id}` | 读取情境、参与者描述、事件与回合状态；不调用模型 |
| POST | `/v2/scenarios/{activity_id}/messages` | 一次练习回合；仅 `ACTIVE`；`{request_id, message, reply_suggestion?}` |
| POST | `/v2/scenarios/{activity_id}/rounds/{request_id}/abandon` | 显式结束未解决的 PENDING 回合，不补写伙伴内容 |
| POST | `/v2/scenarios/{activity_id}/branches` | 创建分支，`201`；仅 `ACTIVE`；`{request_id, after_event_id, message}` |
| GET | `/v2/scenarios/{activity_id}/branches` | 分支摘要列表；不调用模型 |
| GET | `/v2/scenarios/{activity_id}/branches/{branch_id}` | 只读该分支及其事件；不存在 `404 SCENARIO_BRANCH_NOT_FOUND` |
| POST | `/v2/scenarios/{activity_id}/branches/{branch_id}/retry` | 重试 `FAILED` 分支；`{request_id}` |
| POST | `/v2/scenarios/{activity_id}/review` | 生成/重试复盘；仅 `ENDED`；`{request_id}` |
| GET | `/v2/scenarios/{activity_id}/review` | 复盘状态（`NONE/PENDING/READY/FAILED`）与结果；不调用模型 |
| POST | `/v2/scenarios/{activity_id}/adopt` | 显式采用一条 `READY` 分支；`{request_id, branch_id, character_id?}` |

错误码：`404 ACTIVITY_NOT_FOUND` / `SCENARIO_NOT_FOUND` / `SCENARIO_BRANCH_NOT_FOUND`、`409 ACTIVITY_VERSION_CONFLICT` / `ACTIVITY_STATE_CONFLICT` / `ACTIVITY_VERSION_EXHAUSTED` / `REQUEST_ID_CONFLICT` / `SCENARIO_NOT_ACTIVE` / `SCENARIO_ROUND_PENDING` / `SCENARIO_STATE_CHANGED` / `SCENARIO_BRANCH_POINT_INVALID` / `SCENARIO_BRANCH_PENDING` / `SCENARIO_NOT_ENDED` / `SCENARIO_REVIEW_PENDING` / `SCENARIO_REVIEW_NOT_READY` / `SCENARIO_BRANCH_NOT_READY` / `SCENARIO_ADOPT_CHARACTER_REQUIRED` / `SCENARIO_ADOPT_CHARACTER_INVALID` / `SCENARIO_ALREADY_ADOPTED`、`422` 契约校验、`502 SCENARIO_GENERATION_FAILED` / `SCENARIO_BRANCH_GENERATION_FAILED` / `SCENARIO_REVIEW_FAILED`、`503 ACTIVITY_CORRUPT` / `SCENARIO_CORRUPT`（存储内容不符合冻结契约）。

## 10. 明确不做

- 不做跨情境延续、不自动把分支写进现实。DSH 参与见 [DSH_AUTHORIZATION](DSH_AUTHORIZATION.md)，不在此切片扩展。
- 不创建调度器、不自动推进 ACTIVE → 下一步、不产生 Intent、不自动结束/复盘/采用。
- 不复制 Persona/Memory/Mental State，不写第二套人物注册表。
- 不把未采用的练习内容写入真实 Event/Memory/Conversation fact；显式采用只写一条 REAL 事件和一条 REAL `SHARED` 记忆。
- 不把练习合并进 Direct/Group，不自动发送建议草稿，不在练习内产生真实后果。
- 不做多用户/权限模型：与现有本地单操作者部署一致。
