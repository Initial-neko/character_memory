# Activity 生命周期与参与者 (P10)

状态：**IMPLEMENTED — LIFECYCLE ONLY**。本文冻结并描述 Activity 内核的最小可用切片：状态机、参与者身份、版本冲突、幂等和 scope 边界。执行、分支、复盘属于 P12，本切片**不**实现，也不预留第二套运行时。

Activity 是 `Activity → Participants → State → Actions → Consequences → Events → Reflection` 责任链的前三段所有者。当前只有一个持久生命周期，没有任何模型调用、调度器或自动推进。

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

## 5. 读取与恢复

- `GET /v2/activities`、`GET /v2/activities/{activity_id}` 只读本地 SQLite；不加载模型 bundle、不初始化 embedding provider、不写任何行。
- 进程重启后直接读取即可继续：状态、参与者、版本与回执都在 SQLite。恢复一个已存在 Activity 不需要模型调用；验收用真实 HTTP 双进程证明调用数为 0。
- 读取不是执行：本切片没有「继续活动的下一步」，所以读取/恢复既不推进状态也不生成事实。

## 6. 与 experience scope 的关系

- Activity 产出的事实一律是 **SIMULATED**。响应中的 `experience_scope` 由服务端生成，调用方不能提交：
  `{"schema_version": 1, "reality": "SIMULATED", "activity_id": "<id>"}`。
- 该 scope 被现有 `domain/experience_scope.py` 的真实边界拒绝，因此活动事实不会进入 Direct/Group 的真实认知、真实 Memory 召回或关系认知。
- `branch_id` 只由 `activity_experience_scope(..., branch_id=...)` 在 P12 有真实分支时写入；P10 从不生成它，也不接受调用方提供。
- 本切片不写 Event/Memory/Conversation fact。scope 契约先冻结，是为了让 P12 的产出在写入时只有一种正确形状。

## 7. HTTP 表面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v2/activities` | 创建，`201`；未知正式人物或纯模拟阵容 `422`；`request_id` 冲突 `409` |
| GET | `/v2/activities?limit=&status=` | 列表，最近更新优先，`limit` 1–100 |
| GET | `/v2/activities/{activity_id}` | 读取；不存在 `404 ACTIVITY_NOT_FOUND` |
| POST | `/v2/activities/{activity_id}/pause` | `{request_id, expected_version}` |
| POST | `/v2/activities/{activity_id}/resume` | 同上；`CREATED` 与 `PAUSED` 均可 |
| POST | `/v2/activities/{activity_id}/end` | 同上 |

错误码：`404 ACTIVITY_NOT_FOUND`、`409 ACTIVITY_VERSION_CONFLICT` / `ACTIVITY_STATE_CONFLICT` / `ACTIVITY_VERSION_EXHAUSTED` / `REQUEST_ID_CONFLICT`、`422` 契约校验、`503 ACTIVITY_CORRUPT`（存储内容不符合冻结契约）。

没有 UI：这是后端切片，前端入口需要单独批准的桌面/移动参考图。

## 8. 明确不做

- 不执行模型、不开分支、不做复盘（P12）。
- 不创建调度器、不自动推进 ACTIVE → 下一步、不产生 Intent。
- 不复制 Persona/Memory/Mental State，不写第二套人物注册表。
- 不把 Activity 结果写入真实 Event/Memory；P12 写入时必须带本切片冻结的 SIMULATED scope。
- 不做多用户/权限模型：与现有本地单操作者部署一致。
