# Activity 生命周期、关系练习与参与者 (P10 + P11)

状态：**IMPLEMENTED — LIFECYCLE + RELATIONSHIP PRACTICE**。本文冻结 Activity 内核，以及 P11 在其上实现的关系练习执行切片：用户创建 2–3 位伙伴的模拟情境，每位伙伴给出可辨认、允许分歧的观点，模拟人物合理回应，用户复用既有建议/编辑/自由输入发送。分支、复盘和跨情境延续仍属于 P12，本文不实现，也不预留第二套运行时。

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
- `branch_id` 只由 `activity_experience_scope(..., branch_id=...)` 在 P12 有真实分支时写入；当前不生成它，也不接受调用方提供。
- P10 不写任何事实；P11 只写自己的 `scenario_events` 行，每行都带冻结 scope。它仍不写 Event、Memory 或 Direct/Group conversation fact。scope 契约先冻结，是为了让 P12 的产出在写入时只有一种正确形状。

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

## 8. HTTP 表面

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

错误码：`404 ACTIVITY_NOT_FOUND` / `SCENARIO_NOT_FOUND`、`409 ACTIVITY_VERSION_CONFLICT` / `ACTIVITY_STATE_CONFLICT` / `ACTIVITY_VERSION_EXHAUSTED` / `REQUEST_ID_CONFLICT` / `SCENARIO_NOT_ACTIVE` / `SCENARIO_ROUND_PENDING` / `SCENARIO_STATE_CHANGED`、`422` 契约校验、`502 SCENARIO_GENERATION_FAILED`、`503 ACTIVITY_CORRUPT` / `SCENARIO_CORRUPT`（存储内容不符合冻结契约）。

## 9. 明确不做

- 不开分支、不做复盘、不做跨情境延续（P12）。
- 不创建调度器、不自动推进 ACTIVE → 下一步、不产生 Intent。
- 不复制 Persona/Memory/Mental State，不写第二套人物注册表。
- 不把练习内容写入真实 Event/Memory/Conversation fact；P12 写入时必须带本切片冻结的 SIMULATED scope。
- 不把练习合并进 Direct/Group，不自动发送建议草稿，不在练习内产生真实后果。
- 不做多用户/权限模型：与现有本地单操作者部署一致。
