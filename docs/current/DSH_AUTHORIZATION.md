# DSH 授权参与与执行契约 (P13)

状态：**CONTRACT FROZEN — NOT IMPLEMENTED**。本文只冻结外部主体 DSH 的授权参与与执行边界；本轮不写代码、不建表、不挂路由、不改 UI。验收以第 10 节为准，未实现期间 `STATUS` 保持 `CONTRACT FROZEN`。

DSH 指由外部操作者持有、在本系统之外运行的数字主体。它不是 Person，不是用户，也不是 Activity 内的 `SIMULATED_CHARACTER`。本契约把 DSH 当作需要显式授权、可撤销、可审计的第三方主体；默认拒绝一切访问。

现状依据：

- 当前 HTTP 安全边界是私有 Tailnet + Serve，没有应用级按客户端认证；按设备 bearer token 只是 PROPOSED（`docs/current/MOBILE_API_CONTRACT.md:34`）。
- 当前只有本地单操作者；现有 profile/goal API 明确不是多用户认证，也不是 DSH grant（`docs/current/PERSON_RUNTIME.md:716`、`docs/current/PERSON_RUNTIME.md:961`）。
- Activity 只承认 `CHARACTER` 与 `SIMULATED_CHARACTER` 两种参与者，数据库 CHECK 也把它们写死（`src/character_memory/domain/activity.py:9`、`src/character_memory/storage/activities.py:84`）。
- Activity 产出一律为 SIMULATED，且当前不写 Event/Memory（`docs/current/ACTIVITY.md:63`、`docs/current/ACTIVITY.md:67`、`docs/current/ACTIVITY.md:89`）。
- 既有能力执行器已经采用“先预留、后执行、失败也计费、回执幂等”的语义，可作为 P13 的实现先例，但 DSH 不复用其角色授权（`src/character_memory/runtime/capability_execution.py:258`、`:267`、`:277`、`:280`、`:297`）。

## 0. 不变量

以下每条都是不可协商的硬约束，违反即视为 P13 未实现：

1. **身份不合并**：DSH 永远不获得 `character_id`、`user` 身份或 `sim:` 命名空间；三者任一被混用即失败。
2. **默认拒绝**：没有显式 scope/action/未过期 grant 的请求一律失败，不存在“空 scope 等于只读”或“注册即可用”。
3. **服务端身份**：动作的 actor、grant、scope、expiry、SIMULATED scope 全部由服务端从认证上下文派生；请求体不得提供这些字段。
4. **单一写入口**：DSH 只能经 HTTP 公共服务写入；不得持有数据库路径、SQL 连接、Persona 目录、Provider 密钥或任意文件/Shell 权限。
5. **写入不越界**：P13 的 DSH 动作只写 Activity 事实与 DSH 授权/审计表；不写 `memories`、`events`、`mental_states`、`intents`、Persona、Space、Group、Direct。
6. **可撤销且即时**：撤销后下一次请求立即失败；不依赖进程重启、缓存过期或后台任务。
7. **全量审计**：认证失败、拒绝、读取、动作、授权、撤销都必须有不可通过公共 API 删除的审计记录。
8. **SIMULATED 必须显式**：DSH 产物必须携带完整的 SIMULATED scope。由于既有边界把“缺失 scope”当作 legacy REAL（`src/character_memory/domain/experience_scope.py:8`），缺失 scope 的 DSH 事实不是“未标注”，而是错误，必须 fail closed。

## 1. 外部主体：识别、注册、吊销

### 1.1 身份元组

一个 DSH 主体由以下不可变字段标识：

| 字段 | 规则 |
| --- | --- |
| `subject_id` | 服务端生成，格式 `dsh_` + 32 位小写十六进制；不可由调用方指定、不可修改、不可复用。 |
| `display_name` | 操作者提供的展示名，1–60 字符；不是身份，不参与鉴权、不唯一、不得被解析为 Persona/ID。 |
| `key_id` | 服务端生成，格式 `key_` + 16 位小写十六进制；每个主体最多 2 个 ACTIVE key。 |
| `public_key` | Ed25519 公钥，32 字节，base64url 无填充；全零公钥拒绝；同一公钥不得同时属于两个 ACTIVE 主体。 |
| `status` | `ACTIVE` / `REVOKED`；主体级状态与 key 级状态独立。 |

身份只由 `(subject_id, key_id, 私钥签名)` 证明。`display_name` 与 Persona 名、`character_id`、Space 作者名、`user` 之间不存在映射。DSH 不得以 `character_id=user`、`actor_type=USER` 或 `recipient_id=user` 出现在任何请求或事实中。

### 1.2 注册（仅操作者）

管理面使用独立 operator token，不使用 DSH 签名：

```http
POST /v2/dsh/admin/subjects
X-DSH-Operator-Token: <CHARACTER_MEMORY_DSH_OPERATOR_TOKEN>
Content-Type: application/json

{"request_id": "req-dsh-reg-0001", "display_name": "外部合作者 A",
 "public_key": "<base64url-32-bytes>"}
```

- 未配置 `CHARACTER_MEMORY_DSH_OPERATOR_TOKEN` 时，管理面全部返回 `401 DSH_OPERATOR_AUTH_REQUIRED`，不是“默认本机放行”。Tailscale Serve 会把远端请求表现为 loopback，因此不能用来源 IP 代替 token。
- token 使用常数时间比较；不得写入日志、审计 `detail_json` 或错误响应。
- 成功返回 `201 {subject_id, key_id, status:"ACTIVE", created_at}`；`request_id` 幂等，同 payload 重放返回原结果，不同 payload 返回 `409 DSH_REQUEST_ID_CONFLICT`。
- 注册只建立身份，不建立任何授权。新主体除 `GET /v2/dsh/self` 外没有任何权限。
- 创建 grant 时还必须验证 `subject_id` 已作为 DSH 参与者写入目标 Activity 且 `participation_status=ACTIVE`；否则 `422 DSH_PARTICIPANT_MISSING`。

### 1.3 请求认证

所有 DSH 业务请求带：

```text
X-DSH-Subject:   dsh_<32 hex>
X-DSH-Key:       key_<16 hex>
X-DSH-Timestamp: <RFC3339 UTC>
X-DSH-Nonce:     <16–64 位 [A-Za-z0-9_-]>
X-DSH-Signature: <base64url Ed25519>
```

签名原文按行拼接，逐字节 UTF-8：

```text
DSH1
<METHOD>
<path 与 query，原样>
<sha256(raw request body) 小写 hex；GET 为空体哈希>
<X-DSH-Timestamp>
<X-DSH-Nonce>
<grant_id，无 grant 时为空字符串>
```

校验顺序与失败码：

| 条件 | 结果 |
| --- | --- |
| 缺少任一头、格式错误、签名不匹配 | `401 DSH_AUTHENTICATION_FAILED` |
| 时间戳与服务器时间相差超过 300 秒 | `401 DSH_CLOCK_SKEW` |
| `(subject_id, key_id, nonce)` 在 600 秒内重复 | `401 DSH_NONCE_REPLAY` |
| key 不属于该 subject，或 key 已 REVOKED | `401 DSH_KEY_REVOKED` |
| subject 已 REVOKED | `403 DSH_SUBJECT_REVOKED` |

签名绑定 method、path、body 与 grant；把同一签名改投到另一路径、另一 grant 或另一 body 必须失败。客户端时间只用于签名窗口，不作为授权有效期或配额判定依据。

### 1.4 吊销与轮换

```http
POST /v2/dsh/admin/subjects/{subject_id}/keys/{key_id}/revoke
POST /v2/dsh/admin/subjects/{subject_id}/revoke
```

- key 吊销：只拒绝该 key 的签名；该主体的其他 ACTIVE key 与所有 grant 不变。
- subject 吊销：在**一个 immediate 事务**内把 subject 置 `REVOKED` 并将其所有 grant 置 `REVOKED`；不存在“主体已吊销但旧 grant 仍可用”的中间状态。
- 轮换：先添加新 key，再吊销旧 key；不允许删除 key 行。主体最多 2 个 ACTIVE key，超出返回 `409 DSH_KEY_LIMIT`。
- 吊销幂等：相同 `request_id` 重放原结果；不同 `request_id` 对已吊销对象返回 `200 {status:"REVOKED", already_revoked:true}`，并新增一条审计。
- 被吊销的 `subject_id` 永久不可重新激活；重新授权必须注册新主体、获得新 `subject_id`。

### 1.5 网络与监听器边界

授权规则只有在 DSH 无法绕过 DSH 路由时才成立。当前 Core 把所有 `/v1/*`、`/v2/activities` 与 UI 挂在同一 FastAPI app 上（`src/character_memory/server.py:33` 起），没有任何应用级认证（`docs/current/MOBILE_API_CONTRACT.md:34`）。因此 P13 必须冻结以下部署边界：

- Core 增加一个**独立 DSH 监听器**（独立端口或独立 app），只挂载 `/v2/dsh/*` 与 `/health`；不挂载 `/v1/*`、`/v2/activities`、静态资源、`/docs`、`/openapi.json`。其他路径在 DSH 监听器上一律 `404`。
- DSH 进程只能访问该监听器；现有操作者/UI/Tailscale 监听器不得对 DSH 网络身份可达。若无法做到独立监听器，必须用独立的 OS 用户/防火墙/反向代理实现同等隔离；仅靠“DSH 只调用授权路由”的自觉不构成边界。
- 操作者监听器不得把 `X-DSH-*` 头当作认证；带这些头但没有 operator token 的请求按普通未认证请求处理（不获得任何 DSH 权限）。
- DSH 监听器不得把 operator token 当作认证；管理面只在操作者监听器上提供。
- 验收必须证明 DSH 监听器上的 `/v1/chat/history-page`、`/v2/activities`、`/openapi.json`、`/docs` 均不可达（见 A27）。

## 2. 授权范围

### 2.1 Grant 对象

Grant 是唯一授权载体，字段冻结如下：

| 字段 | 规则 |
| --- | --- |
| `grant_id` | 服务端生成，`grant_` + 32 位小写十六进制。 |
| `subject_id` | 必须存在且 ACTIVE。 |
| `activity_id` | P13 必填；一个 grant 只对一个 Activity 有效，不跨活动复用。主体必须已由操作者写入该 Activity 的参与者列表；P13 不提供 DSH 自行加入 Activity 的接口。 |
| `scopes` | 闭集，见 2.2；不得为空；不得含 `*`。 |
| `actions` | `scopes` 的子集派生；只能出现 5.1 的动作名。 |
| `public_facts` | 可选，显式 `{kind,id}` 列表；P13 仅支持 `SPACE_POST`，最多 20 条。 |
| `expires_at` | 必填绝对 UTC 时间；必须晚于创建时间，最长 7 天。 |
| `max_reads` | 1–1000；默认 200。 |
| `max_actions` | 1–100；默认 20。 |
| `status` | 存储仅 `ACTIVE` / `REVOKED`；过期是派生状态，见第 3 节。 |
| `version` | 单调递增；每次状态变更 +1。 |

创建 grant 是操作者动作：

```http
POST /v2/dsh/admin/grants
X-DSH-Operator-Token: ...

{"request_id":"req-dsh-grant-0001","subject_id":"dsh_...","activity_id":"...",
 "scopes":["ACTIVITY_READ_SELF","ACTIVITY_READ_TRANSCRIPT","ACTIVITY_PARTICIPATE"],
 "actions":["ACTIVITY_SUBMIT_TURN","ACTIVITY_LEAVE"],
 "public_facts":[], "expires_at":"2026-10-29T12:00:00Z",
 "max_reads":200,"max_actions":20}
```

### 2.2 允许的 scope（闭集）

| scope | 允许的读取 | 允许的动作 |
| --- | --- | --- |
| `ACTIVITY_READ_SELF` | Activity 的 id/title/status/version/participants | 无 |
| `ACTIVITY_READ_TRANSCRIPT` | 该 Activity 的事实流（分页） | 无 |
| `ACTIVITY_PARTICIPATE` | 无 | `ACTIVITY_SUBMIT_TURN`、`ACTIVITY_LEAVE` |
| `SPACE_POST_READ` | 仅 `public_facts` 中列出的 Space post 公共字段 | 无 |

`ACTIVITY_PARTICIPATE` 必须同时具备 `ACTIVITY_READ_SELF`，否则创建 grant 返回 `422 DSH_SCOPE_DEPENDENCY`；参与者必须能读到自己的参与状态。

### 2.3 不可授予（硬拒绝）

以下 scope/action 即使操作者提交也返回 `422 DSH_SCOPE_NOT_GRANTABLE`，P13 不提供任何等价旁路：

- 读取 Direct/Group 历史、`conversation_events`、Memory、Mental State、Persona 正文、Intent、Goal、Experience Profile、通知、其他主体的审计。
- 写入或更正 Memory、Event、Mental State、Persona、Goal、Profile、Space/Group/Direct 事实。
- `character_id`/`user` 身份、角色委派、代发消息、代读私聊。
- 任意 SQL、数据库文件、文件系统、Shell、环境变量、Provider/API 密钥、模型调用、浏览器或网络工具。

这些都是本契约的产品/安全边界，不是实现待办。若未来要开放，必须新增独立契约与迁移，不能靠新增一个字符串 scope 实现。

### 2.4 判定顺序

每个业务请求按固定顺序判定，任一失败立即返回且不产生业务副作用：

```text
签名/时间/nonce -> subject ACTIVE -> key ACTIVE
-> grant 存在且属于 subject -> activity_id 匹配
-> grant status ACTIVE -> server_now < expires_at
-> scope 覆盖 -> action 在 grant.actions 内
-> 请求 schema -> 配额 -> 领域状态（如 Activity 必须 ACTIVE）
-> 预留/写入 -> 审计
```

不得先执行业务逻辑再补检查；不得把“找不到 grant”与“scope 不足”合并成 200 空结果。

## 3. 有效期、配额、重复与晚到

### 3.1 时间

- 所有时间由服务器生成/比较，使用 UTC；客户端提供的 `expires_at` 只用于创建 grant 时的显式输入，之后不可改。
- 有效条件严格为 `server_now < expires_at`。`server_now == expires_at` 即过期。
- 过期无需后台任务：每次请求读取 grant 时派生 `effective_status = REVOKED | EXPIRED | ACTIVE`。不得依赖调度器把行改成 EXPIRED。
- 服务器时间与客户端签名时间相差超过 300 秒按 1.3 拒绝；该检查不消耗配额。

### 3.2 配额

- 读取：每个成功返回 2xx 的读取请求消耗 1 次 `max_reads`；被拒绝、认证失败、404、503 不消耗。重复幂等读取不重复消耗（3.3）。
- 动作：在写入事务内、产生事实之前预留 1 次 `max_actions`；预留与事实、回执、审计同事务提交。事务成功则消耗 1 次；事务回滚则计数恢复，因为 P13 没有已经发生的不可回滚副作用。既有外部能力执行器“调用失败也计费”的语义（`src/character_memory/runtime/capability_execution.py:267`）在这里不适用；未来若加入外部动作，必须改为两阶段并单独冻结失败计费。
- 配额与业务写入在同一 immediate 事务内更新；任一失败整体回滚，不允许“事实写了但计数没加”。
- 计数不可回收、不可由 DSH 重置；操作者只能新建 grant。

### 3.3 重复请求

所有 DSH 读取与动作都必填 `request_id`（8–80 位 `[A-Za-z0-9_-]`，与 Activity 现有 `RequestId` 约束一致，`src/character_memory/domain/activity.py:29`）。

- 唯一键是 `(grant_id, request_id)`，不是全局。两个 grant 可以使用相同 `request_id`。
- 相同 key + 相同 canonical payload：
  - 动作：返回原结果并带 `replayed:true`；不新增事实、不递增配额。
  - 读取：重新读取当前事实并带 `replayed:true`；不消耗配额。读取无副作用，因此不承诺字节级快照一致。
- 相同 key + 不同 payload：`409 DSH_REQUEST_ID_CONFLICT`，不产生副作用。
- 动作的“相同 payload”按服务端规范化后的字段比较，不按原始 JSON 字节比较；未知字段在 schema 阶段已被拒绝。

### 3.4 晚到提交

“晚到”只按服务器接收并取得写锁后的 **admission 时间** 判断，客户端时间戳不构成延期理由：

- `admitted_at` 是取得写锁、在事务内重验授权后、写入事实之前由服务器取的 UTC 时间。
- 请求在 grant 过期后到达：`403 DSH_GRANT_EXPIRED`，即使签名时间在过期之前。
- 请求在过期前进入队列、但取得写锁时已过期：同样拒绝；不得因为“先到队列”而放行。
- `admitted_at < expires_at` 即合法；即使事务提交时间略晚于 `expires_at`，动作仍有效。审计同时记录 `admitted_at` 与 `expires_at`，不能用提交时间反推授权。
- P13 动作只写本地 SQLite：事实、回执、配额、审计在同一事务提交，因此不存在“外部副作用已完成但本地不知道”的窗口。若事务未提交，什么都没发生；若已提交而响应丢失，客户端用同一 `request_id` 重放得到原结果。
- 未来若加入不可回滚的外部副作用，必须单独冻结“预留 -> 外部调用 -> 提交/UNKNOWN”的两阶段契约；P13 不预留该旁路。

## 4. 撤销

### 4.1 即时性

- grant 撤销：`POST /v2/dsh/admin/grants/{grant_id}/revoke`。
- subject 撤销：见 1.4，原子地撤销其全部 grant。
- 撤销提交后，下一次请求在认证/授权阶段即失败：`403 DSH_GRANT_REVOKED` 或 `403 DSH_SUBJECT_REVOKED`。实现不得缓存 grant 状态，不得在进程内保留“已验签即可用”的对象。
- 撤销与动作竞争时，SQLite 写锁给出唯一顺序：
  - 动作先提交：事实存在，随后撤销；事实保留并标注。
  - 撤销先提交：动作在事务内看到 REVOKED 并回滚，不产生事实。
  - 不允许两者都发生一半，或出现“事实存在但无回执/审计”。

### 4.2 已产生副作用的标注

- 事实行保存不可变的 `authorization_json`：`{grant_id, subject_id, status_at_commit:"ACTIVE"}`；不得在撤销时改写事实内容或该字段。
- 读取投影通过 join 当前 grant/subject 状态派生 `authorization_status: REVOKED_AFTER_COMMIT`；审计中记录撤销时间与受影响 grant。
- 撤销后 DSH 自己不能再读取该事实（无有效 grant）；操作者投影仍可读取并看到标注。
- 事实不得被删除、隐藏为不存在或改写成“从未发生”。若产品要求对普通用户隐藏，只能新增读取投影规则，不能删除审计与事实。

## 5. 类型化动作

### 5.1 动作清单（闭集）

P13 只允许两个动作：

| 动作 | 参数 schema | 语义 |
| --- | --- | --- |
| `ACTIVITY_SUBMIT_TURN` | `{type, request_id, content, reply_to_fact_id?}` | 向 Activity 事实流追加一条 DSH 发言；Activity 必须为 `ACTIVE`。 |
| `ACTIVITY_LEAVE` | `{type, request_id}` | 把该 DSH 参与状态置为 `LEFT`；幂等；不结束 Activity，也不自动撤销 grant。Activity 为 `ENDED` 时返回 409；`CREATED`/`ACTIVE`/`PAUSED` 允许离开。 |

`content` 为 1–4000 个 Unicode 码点，去首尾空白后不得为空；`reply_to_fact_id` 必须存在、属于同一 Activity，否则 `422 DSH_SCHEMA_INVALID`。服务端分配 `seq`；DSH 不能提供序号、时间戳、actor、scope 或授权字段。

动作端点：

```http
POST /v2/dsh/grants/{grant_id}/actions
```

请求体不得出现：`character_id`、`user_id`、`actor_id`、`actor_type`、`subject_id`、`grant_id`、`experience_scope`、`reality`、`scope`、`expires_at`、`memory_id`、`persona`、`sql`。出现任一字段返回 `422 DSH_IDENTITY_FIELD_FORBIDDEN`，不落事实。

### 5.2 执行边界

- 动作只经 DSH application service 调用现有 `ActivityRepository` / 新事实仓库的公共方法；不得在路由层拼 SQL，不得把 `store.conn` 暴露给 DSH 模块之外的调用方。
- `ACTIVITY_SUBMIT_TURN` 在一个 immediate 事务内完成：重验授权与 Activity 状态 -> 预留配额 -> 插入事实 -> 写回执 -> 写 `ACTION_RESERVE` 与终态审计 -> 提交。成功路径只产生一个原子提交。
- 成功事务若回滚（写入失败、进程异常），业务行与配额一起回滚；随后必须用一个独立事务追加 `ACTION_FAIL` 审计并返回 5xx。若该审计事务也失败，仍返回 `503 DSH_AUDIT_UNAVAILABLE`，不得把失败当成成功。
- 不调用 `append_event`、`add_memory`、`set_mental_state`、`save_persona`；这些现有入口要求 `character_id` 或 Persona 路径，DSH 没有这些身份（`src/character_memory/storage/sqlite.py:590`、`:745`；`src/character_memory/api_character_service.py:318`）。
- 不调用 `SpaceRepository.add_comment`：其 `actor_type` 仅接受 `CHARACTER`/`USER`（`src/character_memory/space_store.py:632`），DSH 不能冒充用户或角色。
- 动作失败必须区分：授权失败（4xx，不预留，单独写 `ACTION_DENY`）、领域状态冲突（`409 ACTIVITY_STATE_CONFLICT`，不预留，写 `ACTION_DENY`）、写入失败（5xx，业务事务回滚，另写 `ACTION_FAIL`）。I/O 失败不得伪造成“已提交”。

### 5.3 动作产物

活动事实行冻结为：

```json
{
  "activity_id": "...",
  "seq": 7,
  "actor": {"type": "DSH", "subject_id": "dsh_...", "display_name": "外部合作者 A"},
  "content": "...",
  "reply_to_fact_id": null,
  "experience_scope": {"schema_version": 1, "reality": "SIMULATED", "activity_id": "..."},
  "authorization": {"grant_id": "grant_...", "subject_id": "dsh_...", "status_at_commit": "ACTIVE"}
}
```

`display_name` 只用于展示，读取方不得用它做身份判断。

## 6. 读取边界与 SIMULATED/REAL

### 6.1 可读资源

| 端点 | 所需 scope | 返回 |
| --- | --- | --- |
| `GET /v2/dsh/self` | 仅需有效签名 | 主体状态、key 状态、当前 grants 摘要；不返回其他主体信息。 |
| `GET /v2/dsh/grants/{grant_id}/activity` | `ACTIVITY_READ_SELF` | Activity 元数据与参与者。 |
| `GET /v2/dsh/grants/{grant_id}/transcript?after_seq=&limit=` | `ACTIVITY_READ_TRANSCRIPT` | 只含该 Activity 的事实，`limit` 1–50，按 `seq` 升序。 |
| `GET /v2/dsh/grants/{grant_id}/facts/space_post/{post_id}` | `SPACE_POST_READ` 且 `post_id` 在 `public_facts` 中 | 仅 `{id, character_id, author:{id,name,identity,tagline,avatar_url}, content, created_at, visibility, media_items}`；不含评论、点赞者、通知、source_event_id。 |

Space post 必须存在且 `visibility=ACTIVE_CHARACTERS`；否则按不存在处理（404），不因 grant 列了 ID 就绕过可见性。

读取只读 Activity/公共事实与 DSH 授权表；不得初始化模型、embedding、Persona 注册表或运行时 bundle。读取不得写 Person 的 Event、Memory、Intent、Mental State、Trace 或通知。

### 6.2 硬拒绝

即使 grant 携带任何 scope，以下读取都返回 `403 DSH_SCOPE_DENIED`，不得返回空数组伪装成功：

- `/v1/chat/history-page`、Group history、SSE、通知。
- `/v1/characters/{character_id}/memories`、relationship、life、executions、goals、experience-profile。
- Persona 正文、`capabilities.yaml`、Settings、Provider 密钥、Dev/Diagnostics。
- 未列入 `public_facts` 的 Space post/comment、媒体资产、其他 Activity。

### 6.3 SIMULATED/REAL 边界

- Activity 事实的 scope 只能由服务端生成，形状与 `activity_experience_scope` 完全一致：`{"schema_version": 1, "reality": "SIMULATED", "activity_id": "<id>"}`（`src/character_memory/domain/activity.py:167`、`:178`）。
- 由于 `is_real_experience` 把缺失 `experience_scope` 视为 legacy REAL（`src/character_memory/domain/experience_scope.py:8`），DSH 事实缺失或畸形 scope 时：
  - 写入：事务回滚，返回 `503 DSH_SCOPE_BROKEN`。
  - 读取：返回 `503 DSH_SCOPE_BROKEN`，不得按 REAL 返回。
- DSH 事实必须被 `real_experience_sql` 排除（`src/character_memory/domain/experience_scope.py:21`），因此不能进入 Direct/Group 认知、Memory 召回、关系认知或真实 Intent。
- 读取公共 Space post 时，返回其存储的 scope 原值；若为空，必须显式返回 `scope_status:"LEGACY_REAL"`，不得补写或改写该 post。
- DSH 读到 REAL 公共事实后提交的 turn 仍是 SIMULATED；读取不改变来源事实的 reality，也不把 DSH 产物升级为 REAL。
- 读取本身不产生 Person 的“已观察”事实：DSH 读到的内容不会变成某个角色的 Memory 或 Space 浏览记录。

## 7. 审计

### 7.1 记录范围

审计表 `dsh_audit_events` 为 append-only。每个认证请求至少有一条终态记录；动作另有预留记录：

| `event_type` | 触发 |
| --- | --- |
| `SUBJECT_REGISTER` | 主体注册成功 |
| `KEY_ADD` / `KEY_REVOKE` | key 变更 |
| `SUBJECT_REVOKE` | 主体吊销 |
| `GRANT_CREATE` / `GRANT_REVOKE` | grant 生命周期 |
| `AUTH_DENY` | 签名、时间、nonce、key、subject 失败 |
| `ADMIN_DENY` | 管理面 token/参数失败 |
| `READ` / `READ_REPLAY` | 读取终态 |
| `ACTION_RESERVE` | 动作取得配额、准备写入 |
| `ACTION_COMMIT` / `ACTION_FAIL` / `ACTION_DENY` / `ACTION_REPLAY` | 动作终态 |

字段冻结：

```text
id, at, at_epoch,
subject_id nullable, grant_id nullable, request_id nullable,
event_type, outcome, resource_type, resource_id,
request_hash, detail_json,
prev_hash, record_hash
```

- `detail_json` 只存判定所需的结构化信息（reason、scope、quota 前后值、fact_id、seq 等）；不得复制 Memory/Persona/私聊正文、密钥或签名原文。动作内容已在事实表，审计只存 `request_hash` 与长度。
- 审计使用的 `canonical` 与 Activity receipt 相同：`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`（`src/character_memory/storage/activities.py:43`）。
- `record_hash = sha256(canonical({prev_hash, at, at_epoch, subject_id, grant_id, request_id, event_type, outcome, resource_type, resource_id, request_hash, detail_json}))`；`prev_hash` 为全局上一条 `record_hash`，genesis 为 64 个 `0`。`id` 不参与哈希。
- 成功路径的审计与业务写入在同一事务；审计失败则业务回滚并返回 `503 DSH_AUDIT_UNAVAILABLE`。
- 授权/领域拒绝在返回 4xx 前写 `AUTH_DENY`/`ADMIN_DENY`/`ACTION_DENY`；写审计失败时改返回 `503 DSH_AUDIT_UNAVAILABLE`，不能产生“未记录但已答复”的请求。
- 业务事务回滚后，用独立事务写 `ACTION_FAIL`；该事务失败时仍返回 `503`。读取先写 `READ` 审计再返回；审计写失败则 `503`。
- 公共 API 不提供删除/修改审计的路由。保留期见第 11 节；P13 实现不得自动清理。

### 7.2 核对

```http
GET /v2/dsh/admin/audit?subject_id=&grant_id=&after_id=&limit=
GET /v2/dsh/admin/audit/verify
GET /v2/dsh/admin/grants/{grant_id}/facts?after_seq=&limit=
```

`verify` 逐条重算哈希链，返回 `{ok, checked, first_broken_id}`；篡改任一行必须定位到该行。`facts` 返回事实原值，并通过 join 当前 grant/subject 状态附加 `authorization_status`（`ACTIVE` / `REVOKED_AFTER_COMMIT`），供 A18 验收。管理面同样需要 operator token。

## 8. 冻结的数据模型（实现时才建）

迁移账本名冻结为 `dsh/001-authorization`。表名、关键列与约束如下；实现可以增加索引，不得改变语义：

```sql
dsh_subjects(subject_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
  created_at TEXT NOT NULL, revoked_at TEXT, revoked_reason TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL CHECK(version>=1))

dsh_keys(subject_id TEXT NOT NULL REFERENCES dsh_subjects(subject_id),
  key_id TEXT NOT NULL, public_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
  created_at TEXT NOT NULL, revoked_at TEXT,
  PRIMARY KEY(subject_id,key_id))

dsh_grants(grant_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES dsh_subjects(subject_id),
  activity_id TEXT NOT NULL REFERENCES activities(id),
  scopes_json TEXT NOT NULL, public_facts_json TEXT NOT NULL DEFAULT '[]',
  actions_json TEXT NOT NULL, expires_at TEXT NOT NULL, expires_at_epoch INTEGER NOT NULL,
  max_reads INTEGER NOT NULL CHECK(max_reads>=1),
  reads_used INTEGER NOT NULL DEFAULT 0 CHECK(reads_used>=0 AND reads_used<=max_reads),
  max_actions INTEGER NOT NULL CHECK(max_actions>=1),
  actions_used INTEGER NOT NULL DEFAULT 0 CHECK(actions_used>=0 AND actions_used<=max_actions),
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
  version INTEGER NOT NULL CHECK(version>=1),
  created_at TEXT NOT NULL, revoked_at TEXT, revoked_reason TEXT NOT NULL DEFAULT '')

dsh_request_receipts(grant_id TEXT NOT NULL, request_id TEXT NOT NULL,
  request_hash TEXT NOT NULL, operation TEXT NOT NULL, outcome_json TEXT NOT NULL,
  consumed_reads INTEGER NOT NULL DEFAULT 0, consumed_actions INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, PRIMARY KEY(grant_id,request_id))

dsh_nonces(subject_id TEXT NOT NULL, key_id TEXT NOT NULL, nonce TEXT NOT NULL,
  seen_at_epoch INTEGER NOT NULL, PRIMARY KEY(subject_id,key_id,nonce))

dsh_audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
  at_epoch INTEGER NOT NULL, subject_id TEXT, grant_id TEXT, request_id TEXT,
  event_type TEXT NOT NULL, outcome TEXT NOT NULL,
  resource_type TEXT NOT NULL DEFAULT '', resource_id TEXT NOT NULL DEFAULT '',
  request_hash TEXT NOT NULL DEFAULT '', detail_json TEXT NOT NULL DEFAULT '{}',
  prev_hash TEXT NOT NULL, record_hash TEXT NOT NULL UNIQUE)
```

补充约束：`CREATE UNIQUE INDEX dsh_active_public_key ON dsh_keys(public_key) WHERE status='ACTIVE'`；`dsh_nonces` 中超过 600 秒的行可以清理，但不得早于签名窗口结束。

Activity 侧：

- 新表 `activity_facts(activity_id, seq, actor_type, actor_key, content, reply_to_fact_id, experience_scope_json, authorization_json, created_at)`；`UNIQUE(activity_id,seq)`；`actor_type` 闭集增加 `DSH`；P13 只允许 `ACTOR_TYPE='DSH'` 的写入方为 DSH。DSH 行的 `authorization_json` 必须含 `grant_id`/`subject_id`/`status_at_commit`，空对象在写入前拒绝。
- `activity_participants` 当前 CHECK 只允许两种类型（`src/character_memory/storage/activities.py:84`），必须通过迁移 `activity/003-dsh-participants` 重建表，新增 `DSH`、`subject_id` 与 `participation_status`（`ACTIVE`/`LEFT`，非 DSH 行默认 `ACTIVE`）列，并保持原有唯一键与 position 约束。
- `activity_facts.seq` 由存储层在同一写事务内按 `MAX(seq)+1` 分配，单调递增、不回绕；DSH 不能提供或覆盖。
- `participant_key` 对 DSH 固定为 `dsh:<subject_id>`；不得等于任何 `character_id`、`user` 或以 `sim:` 开头。
- Activity 仍需至少一个 `CHARACTER` 参与者（`src/character_memory/activity_web.py:66`）；DSH 不能成为唯一参与者，也不能替代正式人物。
- `ActivityCreate` 的 participant union 增加 `{actor_type:"DSH", subject_id}`；现有 `CHARACTER`/`SIMULATED_CHARACTER` payload 不变。

## 9. 错误码

| HTTP | code | 条件 |
| --- | --- | --- |
| 401 | `DSH_OPERATOR_AUTH_REQUIRED` | 管理面缺少/错误 operator token |
| 401 | `DSH_AUTHENTICATION_FAILED` | 签名/头/格式错误，未知 subject/key |
| 401 | `DSH_CLOCK_SKEW` | 时间戳超出 ±300 秒 |
| 401 | `DSH_NONCE_REPLAY` | nonce 重放 |
| 401 | `DSH_KEY_REVOKED` | key 已吊销或不属于 subject |
| 403 | `DSH_SUBJECT_REVOKED` | 主体已吊销 |
| 403 | `DSH_GRANT_MISSING` | 无该 grant |
| 403 | `DSH_GRANT_REVOKED` | grant 已吊销 |
| 403 | `DSH_GRANT_EXPIRED` | `server_now >= expires_at` |
| 403 | `DSH_ACTIVITY_SCOPE_DENIED` | grant 的 activity 与请求不符 |
| 403 | `DSH_SCOPE_DENIED` | scope 不足或读取硬拒绝 |
| 403 | `DSH_ACTION_NOT_ALLOWED` | 动作不在闭集或不在 grant.actions |
| 403 | `DSH_QUOTA_EXHAUSTED` | reads/actions 用尽 |
| 404 | `ACTIVITY_NOT_FOUND` / `DSH_FACT_NOT_FOUND` | 资源不存在 |
| 409 | `DSH_REQUEST_ID_CONFLICT` | 同 key 不同 payload |
| 409 | `DSH_KEY_LIMIT` | ACTIVE key 超过 2 |
| 409 | `DSH_PARTICIPANT_LEFT` | 已 LEAVE 的主体再次提交 turn |
| 409 | `ACTIVITY_STATE_CONFLICT` | 非 ACTIVE 时提交 turn |
| 409 | `ACTIVITY_VERSION_CONFLICT` | 既有 Activity 生命周期冲突 |
| 422 | `DSH_SCHEMA_INVALID` | 字段/长度/类型错误 |
| 422 | `DSH_IDENTITY_FIELD_FORBIDDEN` | 请求体出现身份/授权字段 |
| 422 | `DSH_SCOPE_UNKNOWN` | 未知 scope |
| 422 | `DSH_SCOPE_NOT_GRANTABLE` | 硬拒绝 scope |
| 422 | `DSH_SCOPE_DEPENDENCY` | `ACTIVITY_PARTICIPATE` 缺读权限 |
| 422 | `DSH_PARTICIPANT_MISSING` | 主体未写入目标 Activity 的参与者列表 |
| 503 | `DSH_AUDIT_UNAVAILABLE` | 审计无法写入 |
| 503 | `DSH_SCOPE_BROKEN` | 已存事实 scope 缺失/畸形 |

错误响应统一 `{"detail":{"code":"...","message":"..."}}`；不得把内部路径、SQL、堆栈或密钥放进 `message`。

## 10. 验收清单

以下每条都必须能在真实 HTTP 进程上断言，不能只做单元 mock。沿用 Activity 现有真实进程/重启测试方式（`tests/test_activity_http.py:115`），并记录模型调用日志计数。

| ID | 断言 | 方法/证据 |
| --- | --- | --- |
| A01 | 管理面无 token 返回 401；错误 token 也是 401；正确 token 注册成功且 `subject_id` 匹配 `^dsh_[0-9a-f]{32}$` | 真实 HTTP + 独立 operator token |
| A02 | 请求体自带 `subject_id`/`character_id`/`actor_type` 被 422 拒绝，且不产生事实 | POST 动作后查事实/审计计数 |
| A03 | 已注册但无 grant 的主体读取 Activity 返回 403 `DSH_GRANT_MISSING` | 注册后立即请求 |
| A04 | 同一签名改投另一 path/body 返回 401；同一 nonce 二次使用返回 401 `DSH_NONCE_REPLAY` | 重放原始请求字节 |
| A05 | 吊销 key 后该 key 401，另一 ACTIVE key 仍可用；吊销 subject 后所有 key 403 | 两个 key 各发一次请求 |
| A06 | 未知 scope、`*`、硬拒绝 scope 均在创建 grant 时 422；不产生 grant 行 | 管理面创建 + DB/响应断言 |
| A07 | grant 只对绑定的 `activity_id` 有效；改 Activity 返回 403 `DSH_ACTIVITY_SCOPE_DENIED` | 两个 Activity 交叉请求 |
| A08 | 无对应 scope 的读取/动作返回 403，且事实、Memory、Event 计数不变 | 前后表计数 |
| A09 | `max_reads=1`：第一次 200，第二次 403 `DSH_QUOTA_EXHAUSTED`；被拒绝的读取不扣减 | 连续请求 |
| A10 | `max_actions=1`：第一次成功动作消耗额度，第二次 403；若第一次事务因写入失败回滚，则额度不消耗且无事实 | 成功路径 + 注入写失败路径各测一次 |
| A11 | 相同 `(grant_id,request_id)` 相同 payload 重放：动作只产生一条事实、`replayed:true`、配额不变、一条 receipt | 两次 POST + 表计数 |
| A12 | 相同 `(grant_id,request_id)` 不同 payload：409 `DSH_REQUEST_ID_CONFLICT`，无新事实 | 两次 POST |
| A13 | `server_now == expires_at` 时拒绝；过期后所有读取/动作 403 `DSH_GRANT_EXPIRED` | 冻结时钟或短 TTL grant |
| A14 | 签名时间早于过期但服务器在过期后收到：403；客户端时间不能延期 | 构造旧 timestamp + 短 TTL |
| A15 | 取得写锁时已过期：事务回滚，无事实、无配额变化；`admitted_at < expires_at` 的动作提交时间可晚于过期，审计同时记录两者 | 事务内注入时钟/锁竞争 |
| A16 | 撤销 grant 后同一进程下一次请求立即 403 `DSH_GRANT_REVOKED`，无缓存窗口 | 撤销后立即请求 |
| A17 | 撤销 subject 在同一事务撤销全部 grant；撤销 key 不撤销 grant | 多 grant 主体 + 查询 |
| A18 | 撤销前已提交的事实仍存在，内容与 `authorization_json` 不变；操作者投影显示 `REVOKED_AFTER_COMMIT`，DSH 无权再读 | 撤销前后读取对比 |
| A19 | `ACTIVITY_SUBMIT_TURN` 写入的事实 scope 精确等于 SIMULATED 三字段；`real_experience_sql` 过滤后为 0；Direct/Group 上下文与 `memories` 均看不到 | 查询事实 + 过滤 + Memory 计数 |
| A20 | 非 `ACTIVE` Activity 提交 turn 返回 409；`ACTIVITY_LEAVE` 幂等且不改变 Activity 状态；LEAVE 后再次提交返回 409 `DSH_PARTICIPANT_LEFT` | 生命周期组合 |
| A21 | 动作后 `memories`、`events`、`mental_states`、Persona 文件哈希均不变；Space/Group/Direct 无新行 | 前后计数/哈希 |
| A22 | DSH 路由不存在 Direct/Group 历史、Memory、Persona、Profile、通知入口；构造 `MEMORY_READ`/`PERSONA_WRITE` 等 grant 返回 422；未列入 `public_facts` 的 Space post 返回 403/404 | 路由枚举 + grant 创建 + 读取请求 |
| A23 | 每次请求恰好一条终态审计；`verify` 返回 `ok:true`；手工篡改一行后 `first_broken_id` 命中该行 | 审计 API + 直接改测试副本 |
| A24 | 进程重启后 grant、撤销、配额、receipt、审计链保持；读取仍零模型调用 | 双进程重启 + 模型调用日志 |
| A25 | 两个并发同 `request_id` 动作只产生一条事实；撤销与动作竞争只出现“先提交后撤销”或“先撤销后拒绝” | 并发线程/连接 |
| A26 | Activity 只有 DSH 参与者时创建 422；DSH 不能调用管理面或创建 Activity | 现有 Activity 创建规则 + 管理面 |
| A27 | DSH 监听器上 `/v1/chat/history-page`、`/v2/activities`、`/openapi.json`、`/docs` 均 404；操作者监听器收到 DSH 头但无 operator token 时不获得 DSH 权限 | 两个监听器各发请求 |

## 11. 需要用户拍板

以下是有产品/安全后果、无法从现有代码推导的选择。本文已给出保守冻结值；改动任一项需要新版本契约，不能实现时临时放宽。

1. **DSH 的运营主体与信任模型**：谁持有私钥？DSH 与 Core 是否同机？是否接受跨机器经 Tailscale 调用？
2. **凭据形式**：当前冻结为 Ed25519 请求签名。是否改为 operator 签发的一次性 bearer token 或 mTLS？若改，1.3 的签名/重放规则需要重写。
3. **TTL 与配额**：当前冻结 2 小时默认、7 天上限、200 次读取、20 次动作。是否按合作关系调整？是否需要按活动类型区分？
4. **读取面**：P13 只允许 Activity 自身与显式列出的 Space post。是否需要开放角色公共 profile、公共 Space comment、RSS/World 观察？每增加一类都需要新的字段级隐私审查。
5. **角色委派**：是否允许 DSH 代表某个正式 Character 说话/行动？当前硬拒绝。若允许，必须解决冒充、Persona 所有权、责任归属与审计身份，不能只加一个 `character_id` 字段。
6. **REAL 升级**：DSH 产物是否可能经操作者确认后升级为 REAL 经验？当前绝不自动升级；需要独立契约定义谁确认、如何标注、如何撤回。
7. **审计保留与删除**：当前要求不自动删除、至少保留 365 天。是否需要法定/隐私删除流程？删除必须保留可验证的墓碑，而不是静默抹行。
8. **管理面 token**：`CHARACTER_MEMORY_DSH_OPERATOR_TOKEN` 的生成、轮换、存放位置；是否允许空值启动（当前不允许管理面）。
9. **撤销后可见性**：普通用户界面是否隐藏已撤销主体的历史事实？当前只标注、不隐藏、不删除。
10. **DSH 监听器的部署形态**：独立端口、独立进程、独立 OS 用户还是反向代理？如果 DSH 与 Core 同机且无法隔离网络/用户，P13 的读取边界不可验收，必须先确定隔离方式。

## 12. 明确不做

- 本轮不实现任何代码、迁移、路由、UI、配置项或测试；本文是契约冻结，不是完成声明。
- Claude Code/其他助手帮忙开发、评审或写文档，都不等于 P13 完成；完成只以第 10 节在合并后的实现上全部通过为准。
- 不实现 DSH 自动注册、自建 grant、自动续期、自动升级 REAL、跨 Activity 复用 grant。
- 不实现 DSH 直接写 Memory/Event/Persona/Mental State/Space/Group/Direct，也不实现任意 SQL/文件/Shell/模型/浏览器工具。
- 不实现 P12 的分支、复盘或通用 Activity 执行器；P13 只冻结 DSH 参与所需的最小事实追加与授权边界。
- 不实现多用户认证；DSH 是外部主体授权，不是把现有单操作者系统改成多租户。
