# 周期反思与证据水位契约 (P08)

状态：**DESIGN FROZEN — NOT EXECUTED**。本文冻结 P08「证据水位与日/周/月反思」的可执行契约：水位维度、触发规则、Goal/Intent 语义边界、复用清单、三档差异、可解释性与验收断言。

本轮只读源码与现有文档，**不写实现、不建表、不改 schema、不调用模型、不写正式数据库**。当前 `main` 没有任何 reflection 表、scheduler、provider purpose 或 API；本文描述的是将来实施时必须满足的契约，不是已交付行为。

依据：V2 工作包表中的 W10 行（`GOAL` issue #644 评论：长目标、日周月反思与成长版本；验收要求「无新数据 0 调用、重复周期幂等、证据完成、版本回滚」）、ADR-4（每日/周/月按新增证据 watermark 触发，不新增无条件付费 cron）、[MEMORY](MEMORY.md) §7「多粒度记忆方向」、以及现有 Goal/Intent/模型预算/收据实现。

## 1. 冻结结论摘要

| 编号 | 冻结项 | 结论 |
| --- | --- | --- |
| F1 | 水位维度 | per `(character_id, kind)`，`kind ∈ {DAILY, WEEKLY, MONTHLY}`；**不是** per topic、per conversation 或全局 |
| F2 | 权威判据 | 已关闭时间窗的**确定性证据签名**；全局 cursor 只用于廉价跳过，从不授予资格 |
| F3 | 无新证据 | 零模型调用、零 provider 请求、零预算消耗，且留一条可读的 `SKIPPED_IDLE` 周期记录 |
| F4 | 时间 | 资格按 `event_time` 与已关闭窗口判断；水位推进按插入 id；二者不得混用 |
| F5 | 幂等 | 主键 `(character_id, kind, window_start)` 唯一；重复/重启返回已存结果，不再付费 |
| F6 | 修正 | Memory 更正/忘记/恢复与来源撤回改变证据签名，在**下一个**周期再次触发；不重开已提交周期 |
| F7 | Goal | 运营者所有的长期目标；反思只能引用，不能创建、推进、完成或归档 |
| F8 | Intent | 下一步行动；反思不是 reaction opportunity，不产出 `intent_candidates` 或 `actions` |
| F9 | 成本 | 复用 `autonomous_model_daily_max` 与 `ModelInvocationBudget`；每角色每日最多 3 次（三档各 1） |
| F10 | 输出 | 有来源、有界的派生记录；本切片不进入 Persona、关系认知、Goal、Intent 或外发动作 |

## 2. 证据水位

### 2.1 维度：per character + per kind

水位按 `(character_id, kind)` 记录，不按 topic、conversation 或全局记录。理由：

- 反思的输入是「这个人物本人真实经历过的证据集合」，而不是某个话题的增量；现有事实表已经按 `character_id` 组织（`storage/sqlite.py:270-277`、`group_store.py:189-199`、`space_store.py:129-155`）。
- 按 topic 记录会让沉默话题各自累计水位并各自付费，直接违反 ADR-4「无新数据 0 模型调用」；按 conversation 记录会把群聊、私聊、Space 拆成互相不可见的多份反思。
- 三档是三个独立周期，因此 `kind` 进入主键；同一天可以既有 daily 又有 weekly/monthly，但每档每窗口只有一行。

### 2.2 水位记录什么

每个 `(character_id, kind)` 持久化：

| 字段 | 含义 | 用途 |
| --- | --- | --- |
| `last_finalized_window_end` | 最近一次已处理窗口的右开边界（存档时刻的 UTC 实例） | 只决定「下一个该处理的窗口」 |
| `event_cursor` | 该人物在 `events` 中最后被纳入证据的插入 id | 廉价跳过；插入顺序也是 `latest_event_id` 的既有语义（`storage/sqlite.py:893-913`） |
| `conversation_cursor_json` | `[{conversation_id, max_event_id}]`，按 conversation 排序 | Group 事实按会话独立自增，不能用一个整数表示 |
| `memory_max_id` | 已被纳入证据的最大 Memory id | 捕获「更正会创建新 Memory 行」（`memory_web.py:144-157`、`sqlite.py:720-732`） |
| `memory_signature` | `ACTIVE(count,sum_id)` + `SUPERSEDED(count,sum_id)` 的规范化摘要 | 捕获没有新行、只有状态翻转的忘记/恢复（`sqlite.py:692-704`） |
| `policy_version` | 产出该水位的反思策略版本 | 策略升级是显式付费重算，不是静默重放 |

**关键约束**：以上 cursor 只用于「这个窗口不可能有新证据时不要读全表」，**不构成正确性判据**。正确性判据是 2.4 的证据签名比较；即使 cursor 落后或超前，重复处理仍由签名与周期主键拦住。

### 2.3 什么算「新增或修改的证据」

一个已关闭窗口 `[start, end)` 的证据集合由下列各项的并集构成，全部要求来源通过现有 REAL 边界：

1. **新的 Direct/World/Space 感知事件**：`events` 中 `character_id` 匹配、`start <= event_time < end`、`event_time <= now` 的行。Space 的 `SPACE_POST_SEEN` / `SPACE_COMMENT_RECEIVED` 已经是普通事件（`space_autonomy.py:821-841`、`1048-1061`），自己的 `SOCIAL_POST` 也是（`space_autonomy.py:596-612`），因此不需要读 `space_*` 事实表。
2. **新被见证的 Group 事实**：`conversation_events` 中同一窗口内、且满足既有见证规则的行——本人 authored，或存在 `conversation_runtime_traces` 证明本人实际处理过（`group_store.py:520-547`）。仅同群、成员身份或「同一 turn」不算。
3. **窗口内新产生的 Memory**：`memories.id > memory_max_id` 且 `event_time` 在窗口内。Memory 是派生认知，但它携带 Provenance（`domain/models.py:81-93`）；用于让反思看到「人物已经把那件事记成了什么」。
4. **被修正的旧事实**：新 Memory 的 `metadata.corrects_memory_id` 指向窗口外或已提交周期的旧 Memory（`memory_web.py:137-142`）。这是「修正旧事实必须能再次触发」的落点。
5. **状态被改变的证据**：`memory_signature` 与上次水位不同；或上一轮反思输出引用过的 FactRef 现在解析为 `CHANGED` / `UNAVAILABLE` / `hidden`（`application/goal_evidence.py:12-27`、`character_relationships.py:325`）。
6. **未来时间事件不进入**：`event_time > now` 的行既不进入窗口，也不推进任何 cursor；等它成为过去后自然落入当时仍开放的窗口（见 R3）。
7. **不计入**：`TIME_TICK`、`ACTION` 收据、`PROACTIVE_INTENT` 自身（它是计划不是经历）、被 `metadata_status=INVALID` 拒绝的事实、`SIMULATED` 经验（`domain/experience_scope.py:5-18`）。这与 `BROWSE_RECONSIDER_EVENT_TYPES` 的取舍不同（`world_activity.py:41-52`），因为反思比浏览决策更严格。

### 2.4 证据签名

对上述集合计算规范化 SHA-256：

```text
signature = sha256(json({
  "kind": kind,
  "window": [start_utc, end_utc],
  "policy_version": policy_version,
  "facts": [[kind, id, conversation_id?], ...] sorted,
  "memories": [memory_id, ...] sorted,
  "governance": [count_active, sum_active_id, count_superseded, sum_superseded_id]
}))
```

规则：

- 签名只覆盖**有界**集合；不序列化正文、embedding、prompt 或模型输出。
- 事实列表按 `FactRef` 域与 id 排序（`domain/facts.py:9-23`），Direct 与 Group 同号 id 不会互相顶替。
- 窗口按 UTC 实例存储；是否使用本地日界见 D1。窗口一旦关闭就不可变。
- 同一输入必须得到同一签名；签名比较发生在模型调用之前，且是确定性的纯读取。

### 2.5 存在哪里

与权威事实同一个 Core SQLite，通过现有迁移账本建立（`storage/sqlite.py:86-95`），**不新建数据库、sidecar 或第二个记忆库**：

```sql
-- 提案；实际列名在实施 PR 冻结，本文件只约束语义
CREATE TABLE reflection_watermarks(
    character_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('DAILY','WEEKLY','MONTHLY')),
    last_finalized_window_end TEXT NOT NULL,
    event_cursor INTEGER NOT NULL,
    conversation_cursor_json TEXT NOT NULL,
    memory_max_id INTEGER NOT NULL,
    memory_signature TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(character_id, kind)
);

CREATE TABLE reflection_periods(
    character_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('RUNNING','COMMITTED','SKIPPED_IDLE','BLOCKED_BUDGET','FAILED','UNKNOWN')),
    reason TEXT NOT NULL,
    evidence_signature TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    output_json TEXT,
    claimed_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY(character_id, kind, window_start)
);
```

`reflection_periods` 同时是运行账本与用户可读解释；不复制事实正文，`evidence_json` 只存 `FactRef` + memory id + 签名组成项。

## 3. 触发规则

调度器注册进现有 `BackgroundServices`（`background_services.py:10-50`，注册先例 `api.py:433`、`rss_runtime.py:35`），每轮对每个 `(character, enabled kind)` 执行纯读取判定。**判定失败不是错误**，而是可解释的终态。

### R1 无新证据 → 零模型调用

若窗口已关闭、证据集合为空或签名等于该周期已处理的签名，则：

- 不构造 prompt，不调用 `ModelInvocationBudget.invoke`，不产生 provider 请求；
- 写入/保留 `reflection_periods.status='SKIPPED_IDLE'`、`reason='IDLE_NO_NEW_EVIDENCE'`；
- 不消耗 `autonomous_model_daily_max` 名额；
- 该周期被视为终态：同一 `(character, kind, window_start)` 只写一行，重复轮询不再写。

### R2 未来时间不能推进水位

- 窗口资格以 `window_end <= now` 为准；窗口未关闭时一律不处理，`reason='FUTURE_WINDOW'`。
- 事件资格以 `start <= event_time < end` 且 `event_time <= now` 为准。
- 任何 cursor 的推进只发生在 `COMMITTED` 事务内，且只取**已进入证据集合**的 id；未来事件的插入 id 不参与 `event_cursor`。
- 因此把 `world_states.current_time` 或系统时钟回拨，既不能提前触发，也不能让水位回退（对齐 `storage/sqlite.py:1327-1334` 的单调写语义）。

### R3 修正旧事实必须能再次触发

`Memory` 更正（新行 + `supersedes`）、忘记/恢复（`active` 翻转）、来源撤回（`hidden` 或事实缺失）都会改变 2.3 的第 4/5 项，从而改变下一周期的签名并允许一次真实调用。已提交的另一周期不重开（D4）；「修正发生在已提交周期内」由下一周期以「被修正的旧引用」形式携带。

### R4 重复周期幂等

- 主键 `(character_id, kind, window_start)` + `INSERT OR IGNORE` 认领，先例是 `space_daily_runs`（`space_store.py:909-940`，主键定义 `186-199`）。
- 已 `COMMITTED` 的周期再次到达：直接返回已存 `output_json`，不发模型、不写新行。
- 两个连接同时认领：只有一个成功；落败方读到 `RUNNING` 并退出，不重试。

### R5 重启不重复付费

- 模型身份固定为 `character_id`、`opportunity_id = reflection:{kind}:{window_start}:{policy_version}`、`step = reflect`，复用 `ModelInvocationBudget` 的 `identity_key` 语义（`runtime/model_execution.py:83-150`）。
- 身份已 `SUCCESS`：直接返回 receipt 中的结果（`model_execution.py:106-111`），provider 调用数为 0。
- 身份已是 `STARTED`：视为在途/未知，**不重放**（`model_execution.py:125-126`），周期落为 `UNKNOWN`，等待人工处理。
- 身份已 `FAILED`：同周期不自动重试；下一周期是新身份。
- 模型成功但周期提交失败（进程死亡等）：重启后 receipt 重放结果并补提交，仍为 0 次 provider 调用。

### R6 预算

- 每次真实调用前先经过共享 `autonomous_model_daily_max`（`config.py:299-302`、`config.example.yaml:434`、`settings_store.py:250`）；`0` 表示禁用，调用前即返回 `BUDGET_EXHAUSTED`（`model_execution.py:112-117`），周期落为 `BLOCKED_BUDGET` 且不推进水位。
- 预算按真实 UTC 日结算（`model_execution.py:95`），窗口日界是另一回事（D1）。
- 每角色每本地日最多 3 次（三档各 1）；这是工程上限，不是已测量值。

### R7 不得自我放大

- 反思输出不产生 `actions`、不产生 `intent_candidates`、不进入 Intent admission（对照 `person_runtime.py:311-402`）或派发循环。
- 周期记录不是 Event，不触发 `BROWSE_RECONSIDER_EVENT_TYPES`、Proactive 冷却或 Group autonomy。
- 反思不写 `runtime_traces`（该表以 source Event 唯一约束，`sqlite.py:330-336`），自己的账本在 `reflection_periods`。

### R8 单次调用契约

一次反思是**一次**受控 structured 调用：输入为有界证据投影（复用 `runtime/prompt_parts.py:9-21` 的时间锚点与来源格式），输出为有界 JSON（≤ `ModelInvocationBudget.MAX_RESULT_BYTES = 65536`，`model_execution.py:50`）。不做 schema repair 的额外付费轮次；失败即失败。

### R9 可失败、不可伪造

模型或 provider 失败时只记录 `FAILED`/`UNKNOWN` 与错误类别，不生成占位反思、不把空输出写成「这天很平静」。这与既有「失败不是成功沉默」的边界一致（`CONVERSATION_RUNTIME.md:146-151`）。

## 4. Goal 与 Intent 的语义边界

| 维度 | Goal | Intent |
| --- | --- | --- |
| 定义 | 长期目标与有序阶段 | 下一步可执行行动 |
| 谁创建 | 本地运营者显式请求 | 模型在 reaction 中提出，经服务端准入 |
| 持久化 | `person_goals` / `person_goal_stages` / `person_goal_receipts`（`storage/person_goals.py:20-23`） | `intents`（`storage/sqlite.py:304-315`） |
| 时间语义 | 无到期时间；阶段按接受顺序推进 | `earliest_at` / `expires_at` 有界（`domain/models.py:254-258`） |
| 完成凭据 | `confirmation=OPERATOR_ACCEPTED` + 1-8 个不同 `FactRef`，并在提交事务内重验（`domain/goals.py:48-56`、`person_goals.py:106-111`） | 执行/延期/放弃由到期 reaction 决定 |
| 反思的关系 | 可引用为上下文；**不可**创建/暂停/完成/归档 | 不可产出；反思轮不是 opportunity |

强制规则：

- **G1** 反思输出即使写着「目标已完成」，也不能产生 Goal 状态变化；唯一入口是运营者调用 `POST /v1/characters/{id}/goals/{goal_id}/actions`（`goal_web.py:26-30`）。
- **G2** 反思不能把 Intent 当成目标，也不能把 Goal 当成行动；两者在输出 schema 中都不是字段。
- **G3** 反思可作为 Goal 阶段证据的**候选来源**展示给运营者，但 `evidence_digest` 只能由运营者提交时计算（`application/goal_evidence.py:12-27`），反思记录本身不是 `FactRef`。
- **G4** Intent 的存在、数量或到期不构成 Goal 进展；Goal 的阶段数不改变 Intent admission 的地板/配额/去重（`person_runtime.py:49-63`、`311-402`）。

## 5. 日/周/月三档差异

| 项 | DAILY | WEEKLY | MONTHLY |
| --- | --- | --- | --- |
| 窗口 | 一个已关闭本地日 `[local 00:00, +1d)` | 最近 7 个已关闭本地日 | 上一个已关闭日历月 |
| 触发时机 | 日界后；`window_end <= now` | 周界后 | 月界后 |
| 证据量上限 | 最多 200 条事实引用 + 200 条 Memory | 最多 400 条事实 + 400 条 Memory | 最多 600 条事实 + 600 条 Memory |
| 输入字符预算 | 6000 字符证据投影 | 8000 | 10000 |
| 输出形态 | 第一人称「这天发生了什么、我想到什么」，≤ 1200 字符 | 模式/重复/变化摘要 + 引用，≤ 2000 字符 | 趋势/冲突/前后不一致摘要 + 引用，≤ 3000 字符 |
| 输出结构 | `summary`, `evidence_refs[]`, `open_questions[]` | `patterns[]`, `changes[]`, `evidence_refs[]` | `drifts[]`, `conflicts[]`, `evidence_refs[]` |
| 调用次数 | 每角色每日 ≤ 1 | 每角色每周 ≤ 1 | 每角色每月 ≤ 1 |
| 预算消耗 | 1 个 `autonomous_model_daily_max` 名额 | 同 | 同 |
| 派生层级 | 直接读事实 | 见 D3（默认直接读事实 + 可引用 daily 结果） | 见 D3（默认直接读事实，不链式放大） |

三档共享同一套水位、签名、幂等、预算与解释字段；差异只在窗口、上限与输出 schema。任何一档都不能因为另一档运行过而跳过自己的窗口。

## 6. 复用清单（不新建并行体系）

| 能力 | 现有实现 | 复用方式 | 禁止 |
| --- | --- | --- | --- |
| 预算 + 持久调用收据 | `runtime/model_execution.py:48-150`（`invoke`）、`153-183`（校验读取）、`186-212`（状态投影）、`215-241`（适配器） | 反思作为新的 `opportunity_id/step` 使用同一预算；`STARTED` 不重放 | 另建计数表、另建重试循环 |
| 预算配置 | `config.py:299-302`、`config.example.yaml:426-434`、`settings_store.py:250` | 共享 `autonomous_model_daily_max`（D6） | 新增未在 config.example/帮助文本里出现的隐藏开关 |
| LLM 用量归因 | `llm/usage.py:51-97`（scope）、`99-163`（前缀推断）、`403-425`（record） | reflection 调用显式设置 `feature='REFLECTION'`、`purpose='REFLECTION_DAILY|WEEKLY|MONTHLY'` | 落进 `OTHER` 或借用 `WORLD_*` |
| Dev 用量标签 | `web/dev.js:86-145`、`llm/usage.py:209-230`（`llm_calls` schema） | 实施时补三条 purpose 标签 | 只加后端 purpose 不加标签，Dev 显示原始英文 |
| 插入顺序水位 | `storage/sqlite.py:893-913`、`915-939`；`world_activity.py:1652-1673` | 照搬「插入 id 与 `event_time` 是两条时间线」的原则 | 用 `event_time` 比较推进水位 |
| 周期认领/幂等 | `space_store.py:909-940`、`group_store.py:640-830`（durable state/runs） | 复合主键 + `INSERT OR IGNORE` 认领 | 内存字典、进程内锁作为唯一保证 |
| 运行账本 | `world_activity.py:379-392`（schema）、`714-830`（begin/finish/latest/count） | 相同形状的最小账本，独立表；状态语义对齐 | 复用 `world_activity_runs` 污染 World 命名空间 |
| 空闲门控先例 | `world_activity.py:1687-1728`、`1730-1732`、`1938-1960` | 「到期 ≠ 必须付费」的判定先写、调用后置 | 无条件 cron |
| FactRef / 证据核验 | `domain/facts.py:9-23`、`application/goal_evidence.py:12-27`、`domain/experience_scope.py:5-18` | 输出只存引用；`CURRENT/CHANGED/UNAVAILABLE` 复用同一判定 | 复制事实正文进反思记录 |
| Group 见证规则 | `group_store.py:520-547`、`913-938` | 只有 authored 或 trace 证明才算看到 | 用成员身份/同 turn 推断 |
| Memory 治理 | `memory_web.py:90-166`、`storage/sqlite.py:692-704`、`720-732`、`258-264` | 更正/忘记/恢复是证据变化来源 | 反思自己写 Memory 或删事实 |
| Memory admission | `runtime/person_runtime.py:194-309`、`724-737`、`28-29` | 输出若要入 Memory 必须走同一 admission（是否入见 D2） | 绕过 importance/去重直接写库 |
| 未来屏障 | `memory/recall.py:34-39`、`runtime/person_context.py:60-69`、`storage/sqlite.py:813-838` | 证据选择沿用 `event_time <= now` | 把未来事实当已发生 |
| 目标 | `domain/goals.py:10-56`、`storage/person_goals.py:14-114`、`goal_web.py:6-30` | 只读引用；完成仍走运营者 API | 反思改 Goal |
| Intent | `domain/models.py:254-304`、`person_runtime.py:311-487`、`storage/sqlite.py:1192-1207` | 边界对照；反思不产出 Intent | 反思复用 Intent 表存「想法」 |
| Prompt 片段 | `runtime/prompt_parts.py:9-21`、`24-30` | 证据渲染保留时间锚点与来源 id | 自造另一套记忆文本格式 |
| 后台生命周期 | `background_services.py:10-88`、`api.py:433`、`rss_runtime.py:35` | 注册一个 worker，stop 先于共享 store 关闭 | 自带线程池/无限循环 |
| 时钟 | `application/clock.py:7-25`；`activity_schedule.py:35-52` | 用可注入 real clock；不读 `world_states` 模拟时间 | 用事件时间当调度时间 |
| 只读检查面 | `character_life_web.py:52-66`、`character_life.py`（只读投影） | 反思读取接口沿用同一 route 风格 | 新增可写公开 API |
| LLM 使用观察 | `SOCIAL_WORLD.md:890-896` | 在 `GET /v1/world/activity/status` 不混入反思；单独 `reflections` 投影 | 把反思预算伪装成 World 预算 |

## 7. 可解释性

用户必须能回答两个问题：**为什么这次整理发生了**、**依据了哪些事实**。

### 7.1 只读接口（提案，未实现）

```text
GET /v1/characters/{character_id}/reflections?kind=DAILY|WEEKLY|MONTHLY&limit=1..50
→ {
    character_id,
    read_only: true,
    periods: [{
      kind, window_start, window_end, policy_version,
      status, reason, claimed_at, completed_at,
      evidence_refs: [{kind, id, conversation_id?, memory_id?}],
      governance: {active, forgotten, superseded, unavailable},
      output: {...} | null
    }]
  }
```

规则：

- 只读；不初始化模型、不迁移 schema、不写库（对齐 `goal_web.py` 与 Life 路线）。
- 不返回 prompt、原始模型响应、embedding 或隐藏推理；只返回已持久化的结构化输出、引用和状态。
- `SKIPPED_IDLE` / `BLOCKED_BUDGET` / `UNKNOWN` 与 `COMMITTED` 一样可读，否则「为什么没发生」不可解释。
- 引用状态在读取时按当前事实实数计算 `CURRENT / CHANGED / UNAVAILABLE`，不返回缓存的旧正文（对齐 `person_goals.py:50-57`）。
- 若将来在 Life 时间线展示，必须复用只读投影并另做视觉验收；本切片不做 UI。

### 7.2 原因码

| reason | 含义 |
| --- | --- |
| `DISABLED` | 总开关关闭 |
| `FUTURE_WINDOW` | 窗口尚未关闭 |
| `FIRST_PERIOD` | 该档第一次运行 |
| `NEW_EVIDENCE` | 签名与上次不同，允许一次调用 |
| `IDLE_NO_NEW_EVIDENCE` | 窗口已关闭且无证据/签名未变 |
| `PERIOD_ALREADY_COMMITTED` | 重复周期，返回已存结果 |
| `BUDGET_EXHAUSTED` | 共享日预算用尽 |
| `LATE_EVIDENCE_OUTSIDE_WINDOW` | 证据时间落在已提交窗口；本切片不重开（D4） |
| `MODEL_FAILED` / `MODEL_UNKNOWN` | 调用失败或结果未知 |

### 7.3 观测

- `llm_calls.purpose` 必须是 `REFLECTION_DAILY` / `REFLECTION_WEEKLY` / `REFLECTION_MONTHLY`，不得落入 `OTHER`。
- Dev LLM Usage Explorer 同步补标签；这是配置契约的一部分（AGENTS.md 要求每个 purpose 可解释）。
- 日志只记录 character、kind、window、status、reason、引用数、耗时与字符数，不记录正文。

## 8. 不包含

- 不实现自动采用：反思结果不写 Persona、不改 Experience Profile、不生成 GrowthVersion。
- 不改 Persona，不做关系状态/关系认知写入（P07/#689 已独立完成，反思可只读引用）。
- 不自动完成或推进 Goal；不把反思当阶段证据。
- 不产出 Intent，不派发主动消息，不生成 Space 内容或对话动作。
- 不实现「每天强制写日记」；无证据就是 `SKIPPED_IDLE`。
- 不重开已提交窗口，不做跨角色共享反思，不做任何可写公开 API。
- 不新增独立数据库、向量库、消息队列或第二个记忆系统。
- 不改变现有 World/Space/Group/Proactive 的任何节奏或预算语义。

## 9. 验收清单

每条都是可执行断言；测试名只是建议，实施 PR 必须逐条给出结果。

| ID | 断言 |
| --- | --- |
| A1 | 窗口关闭、无证据时运行一次：fake model 调用数为 0，`model_invocation_receipts` 行数为 0，`llm_calls` 新增 0，`reflection_periods` 恰有一行 `SKIPPED_IDLE`（对照 `tests/test_world_model_budget.py:12-30` 的断言方式） |
| A2 | 同一 `(character, kind, window_start)` 连续运行两次：第二次 `provider_calls` 不增加，返回的 `output` 与第一次逐字节相同，表中仍只有一行 |
| A3 | 关闭 store 后重开再运行同一周期：调用数不增加；若上次 receipt 为 `SUCCESS` 但周期未提交，本次补提交且调用数仍为 0 |
| A4 | 写入 `event_time = now + 1d` 的事实：不触发、`event_cursor` 不变；推进时钟到该时间后，落在当时开放窗口内可被处理 |
| A5 | 更正一条被上一周期引用过的 Memory：下一周期签名变化，恰好一次新调用；紧接着同一周期再跑，调用数不变 |
| A6 | 忘记（`active=0`）再恢复一条 Memory：`memory_signature` 变化；两个方向各自可触发一次，且无需新事实行 |
| A7 | 两个连接/线程同时认领同一周期：恰好一次 provider 调用，落败方读到 `RUNNING`，无异常外溢 |
| A8 | `autonomous_model_daily_max=0`：调用数为 0，周期 `BLOCKED_BUDGET`，水位 `updated_at` 不变 |
| A9 | 预算为 1 且同一天三档都到期：恰有一次调用，另两档 `BLOCKED_BUDGET`，按固定顺序选择（DAILY→WEEKLY→MONTHLY） |
| A10 | 任意一次反思完成后：`memories`、`intents`、`mental_states`、`person_goals`、`conversation_events` 行数与运行前相同；不存在新增 `ACTION` 事件 |
| A11 | 反思输出不含 `actions` / `intent_candidates` / Goal 状态字段；带这些字段的 provider 结果被拒绝而不是部分采用 |
| A12 | `COMPLETE_STAGE` 缺少 `OPERATOR_ACCEPTED` 或 `FactRef` 仍返回校验错误；反思记录不能作为 `FactRef` 提交 |
| A13 | 每个 `output.evidence_refs` 都能通过 `FactRef` 解析；Direct/Group 同号 id 不串；`SIMULATED` 来源被排除 |
| A14 | 读取已提交周期后，把来源事件标为 `hidden`：再次读取显示 `UNAVAILABLE/CHANGED`，不返回旧正文，周期记录本身不被改写 |
| A15 | 每次运行（含跳过）都能读到非空 `reason`；`COMMITTED` 记录含 `policy_version`、窗口、引用列表 |
| A16 | 证据超限时按固定顺序截断（先旧后新、再按 FactRef 排序），截断数量写入 `governance`，不因超限改用第二次模型调用 |
| A17 | `output_json` 超过 65536 字节时整次调用失败为 `FAILED`，不截断成半份反思 |
| A18 | `llm_calls` 中新行 `purpose` ∈ 三个 REFLECTION 值，`feature='REFLECTION'`，`OTHER` 计数不变 |
| A19 | 三档窗口边界：DAILY 不跨本地日；WEEKLY 恰 7 天且不与上一周重叠；MONTHLY 为上一日历月；`window_end > now` 时一律不运行 |
| A20 | 关闭进程时 worker 在共享 store 关闭前停止（`BackgroundServices.stop_all` 顺序），重复 start 不产生第二个线程 |
| A21 | 反思运行不改变 World Browse、Space、Group autonomy 的任何 durable clock 或 run 行 |

## 10. 需要用户拍板

以下是有真实产品/数据所有权影响的选择，本轮不自行决定。实施 PR 必须先得到结论；未决前相关分支保持未实现。

- **D1 窗口时区**：日/周/月窗口用哪个本地 offset？Life API 用显式 `offset` 分钟（`character_life_web.py:56-66`，默认 480），而预算日固定 UTC（`model_execution.py:95`）。提案：窗口使用配置的固定 offset 并写入周期记录；代价是预算日与窗口日不是同一天。
- **D2 反思输出是否进入 Memory**：是走现有 admission 成为 `DIARY`（`domain/models.py:213-228` 已有类型），还是只作为只读派生记录等运营者确认？提案：本切片只读；入 Memory 另开切片。
- **D3 周/月能否引用日结果**：只读原始事实（可追溯但更贵更长），还是允许把 daily 输出当作有来源的二级材料（更省但存在误差累积）？提案：周可引用 daily，月只读周 + 原始事实引用。
- **D4 已提交窗口是否可重开**：旧窗口在更正/迟到事实后是否允许付费重跑？提案：v1 不重开，只记录 `LATE_EVIDENCE_OUTSIDE_WINDOW`。
- **D5 记录保留与可见性**：反思记录是否出现在 Life 时间线/Dev 面板、用户能否删除、保留多久。提案：只读接口 + Dev 可见，不删除。
- **D6 预算归属**：共享 `autonomous_model_daily_max` 会让反思挤占浏览额度；是否新增独立日上限。提案：先共享，观察真实用量后再决定。
- **D7 Goal 证据建议的归属**：反思发现某阶段似乎已完成时，是只展示线索，还是生成一条待运营者一键采纳的建议（仍不自动完成）？提案：只展示线索。
- **D8 策略升级语义**：`policy_version` 变化会改变身份并产生一次付费重算；是否需要「重算前必须显式确认」？提案：需要。

## 11. 源码索引

- 事实与时间：`storage/sqlite.py:270-277`（events）、`813-838`（list_events 未来屏障）、`893-913`（插入顺序水位）、`1327-1334`（世界时间单调写）；`group_store.py:189-210`（conversation_events/traces）、`520-547`（见证规则）、`913-938`（群活动信号）；`space_store.py:129-199`（Space 事实与 daily run 主键）。
- Memory 与治理：`domain/models.py:81-93`、`213-228`；`storage/sqlite.py:656-663`、`692-704`、`720-732`；`memory_web.py:90-166`；`runtime/person_runtime.py:194-309`、`724-737`。
- Goal：`domain/goals.py:10-56`；`application/goal_evidence.py:12-27`；`storage/person_goals.py:14-114`；`goal_web.py:6-30`。
- Intent：`domain/models.py:254-304`；`runtime/person_runtime.py:49-63`、`311-402`、`453-487`；`storage/sqlite.py:304-315`、`1192-1207`。
- 预算与收据：`runtime/model_execution.py:48-241`；`config.py:298-302`；`config.example.yaml:426-434`；`settings_store.py:249-250`；`llm/usage.py:51-163`、`403-425`。
- 调度与门控：`background_services.py:10-88`；`api.py:433`；`rss_runtime.py:35`；`world_activity.py:379-392`、`714-830`、`1652-1732`、`1938-1960`；`space_store.py:909-940`；`activity_schedule.py:35-52`。
- 只读检查面：`character_life_web.py:52-66`；`character_life.py:288-314`；`person_goals.py:50-57`。
- 相关文档：[MEMORY](MEMORY.md) §7，[PERSON_RUNTIME](PERSON_RUNTIME.md) §10 与 Operator-confirmed long-term Person goals，[SOCIAL_WORLD](SOCIAL_WORLD.md) 模型调用预算段。
