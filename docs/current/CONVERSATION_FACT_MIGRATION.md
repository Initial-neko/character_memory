# Conversation Fact Migration ADR (W3)

状态：**IMPLEMENTED (UNIFIED) — 正式切换未执行**。本文冻结 W3「Direct → 现有 `conversation_events`」的映射、兼容读取、唯一写入口、回填、切写、回滚与正式操作边界；实现已按第 15 节落地，正式切换仍是独立操作步骤。

本文第一版是只读设计（“DESIGN FROZEN”）。当前实现只保留 UNIFIED 单一模式：没有 LEGACY/SHADOW 运行时分叉，也没有 `conversation_fact_cutover` 开关决定读写路径（该表只为迁移账本与回滚记录保留）。旧的 `events` 消息行永久保留但产品代码不再读它写它。差异见第 15 节。

正式迁移不得由代码合并、应用启动、CI 或测试自动触发。

依据：V2 详细 PLAN 的 W3 定义与 ADR-1 推荐（Direct 从 `events` 渐进迁移到已有 `conversation_events`），以及当前 main 的事实表、引用表和读写路径。

## 1. 现状与问题

### 1.1 两张事实表

当前 Direct 与 Group 的可见对话事实分属两张表：

```text
Direct fact        -> events
Group shared fact  -> conversation_events
```

这个所有权声明见 `docs/current/MEMORY.md:7-17`。

`events` 的真实结构：

```sql
CREATE TABLE events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_time TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
```

见 `src/character_memory/storage/sqlite.py:270-277`。`event_time_epoch` 由 `core/002-epoch-time-keys` 追加（`sqlite.py:174-193`）；核心索引见 `sqlite.py:221-233`。

`conversation_events` 的真实结构：

```sql
CREATE TABLE conversation_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_time TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
```

见 `src/character_memory/group_store.py:170-199`。`event_time_epoch` 由 `group/001-epoch-time-keys` 追加（`group_store.py:63-78`），索引与归档列见 `group_store.py:80-102`。Group 事件还有 `conversations`、`conversation_members`、`conversation_runtime_traces`（`group_store.py:170-210`）。

### 1.2 现有写入

Direct 用户消息和角色消息都写 `events`：

- 用户消息构造：`src/character_memory/application/chat_service.py:62-80`；
- 异步接受先持久化用户事实：`src/character_memory/async_web.py:292-299`；
- Direct 角色消息：`src/character_memory/runtime/person_runtime.py:787-795`；
- Direct ACTION 收据仍写 `events`：`person_runtime.py:847-864`；
- 底层入口：`SQLiteStore.append_event`（`storage/sqlite.py:562-572`）。

Group 用户/角色消息写 `conversation_events`：

- 用户事件构造：`src/character_memory/application/group_conversation_service.py:118-141`；
- 角色事件提交：`group_conversation_service.py:627-640`；
- 底层入口：`GroupRepository.append_event`（`group_store.py:477-500`）。

### 1.3 现有读取与引用

Direct 可见历史、搜索、运行上下文各自直接读 `events`：

- 历史分页：`src/character_memory/storage/chat_history.py:34-59`；
- 历史 API：`src/character_memory/history_web.py:85-117`；
- 旧历史摘要：`src/character_memory/api.py:273-291`；
- 搜索：`src/character_memory/storage/message_search.py:32-69`；
- 最近聊天：`person_runtime.py:157-166`；
- PersonContext 最近事件：`src/character_memory/runtime/person_context.py:50-57`；
- 生命周期/关系/建议：`character_life.py:288`、`character_relationships.py:30-47`、`application/reply_suggestions.py:50-80`。

外部引用大量保存裸整数 Direct Event ID：

- `memories.source_event_id`、`mental_states`/`mental_state_history`、`intents`、`runtime_traces`（`storage/sqlite.py:278-336`）；
- `call_invitations.source_event_id`（`src/character_memory/call_invitations.py:30`）；
- `direct_image_generation_jobs.source_event_id`（`src/character_memory/image_generation_store.py:11-12`）；
- `message_acceptance_receipts.source_ref_json`（`src/character_memory/storage/message_receipts.py:30-35`）；
- `conversation_reaction_receipts.source_ref_json`（`message_receipts.py:106-115`）；
- `reaction_delivery_sources.ref`（`src/character_memory/storage/reaction_delivery.py:27-34`）。

`FactRef` 只用 `EVENT` / `CONVERSATION_EVENT` 区分域，并要求 Group ref 带 `conversation_id`（`src/character_memory/domain/facts.py:9-23`）。语音更新还记录了 Direct/Group 两套 AUTOINCREMENT 序列同号会写错行的真实风险：`src/character_memory/application/voice_message_service.py:51-61`。

### 1.4 问题

1. Direct 与 Group 的“对话事实”有两个 owner，跨渠道的稳定引用只能靠 `FactRef.kind` 区分。
2. Direct 的 `conversation_id` 存在 `metadata_json` 中（`storage/sqlite.py:915-939`），不是一等列；同号 Direct/Group event ID 会重叠。
3. 历史、搜索、SSE、语音、Memory 来源、收据/投递各自读表；任何一处漏改都会出现“有事实但不可见”或“写错行”。
4. 直接把 Direct 行搬进 `conversation_events` 会让旧 ID 与新 ID 冲突，也会让只存裸整数的旧引用表无法判断域。

## 2. 迁移范围与非目标

### 2.1 本 ADR 的迁移对象

**只迁移可见对话事实**：`USER_MESSAGE`、`CHARACTER_MESSAGE`。

理由：

- 这两类正是历史、搜索、SSE、语音和回复建议读取的消息（`history_web.py:85-117`、`message_search.py:32-40`、`message_search.py:83-85`、`conversation_runtime` 的 SSE 路径）。
- 其余 Direct 表行不是“对话消息”：`TIME_TICK`、`PROACTIVE_INTENT`、`VISUAL_OBSERVATION`、`ACTION`、`LIFE_EVENT`、`DIARY`、`SOCIAL_POST`、`SPACE_*`、`WORLD_OBSERVATION`；它们各自仍有运行时/工具/观察 owner，迁移它们会扩大 W3 并破坏现有 trace/意图/收据。
- Direct 角色消息可以引用未迁移的源事件（如 `VISUAL_OBSERVATION`）；迁移后的 `source_event_id` 仍按第 4.5 节的 ID 规则解析，不需要把源事件一起搬走。

该边界需用户确认，见第 12 节。

### 2.2 本轮不包含

- 不执行任何迁移、回填、切写或回滚。
- 不修改 `src/`、`tests/`、配置、数据库或正式数据。
- 不新增/修改表、列、索引、触发器；本文只冻结将来实施时的契约。
- 不改变 Direct/Group API、SSE 事件名、语音合成流程或 Memory admission。
- 不停止/重启任何服务，不调用模型，不访问网络。
- 不把 `events` 的历史消息行删除或改写。

## 3. 目标模型

### 3.1 一张物理表，两个域

目标：所有**对话事实**最终只在一个物理表 `conversation_events` 中；Direct 与 Group 仍由域区分，而不是靠表名区分。

冻结语义：

- `FactRef.kind = "EVENT"` 表示**角色私有/Direct 对话事实域**；
- `FactRef.kind = "CONVERSATION_EVENT"` 表示**共享 Group 对话事实域**；
- `kind` 是域与权限判别，不是物理表名。迁移后 Direct 事实物理上位于 `conversation_events`，但仍是 `EVENT` 域。
- 不新增第三种 `FactRef.kind`；否则 W1/W2 已落库的 receipt、delivery、Memory provenance 全部要改。

Direct 行在 `conversation_events` 中通过 `conversations.type='DIRECT'` 识别；Group 行通过 `type='GROUP'` 识别。群列表、群归档与群成员接口已经过滤 `type='GROUP'`（`group_store.py:446-468`），但并非所有全表查询都过滤；第 5.3 节列出必须补齐的位置。

### 3.2 ID 空间与跨表消歧

冻结常量：

```text
CONVERSATION_FACT_ID_FLOOR = 1_000_000_000_000_000  # 10^15
```

规则：

1. 迁移前，所有既有 `events.id` 和 `conversation_events.id` 必须 `< FLOOR`；否则迁移中止，不得猜测或截断。
2. 回填 Direct 事实使用确定性 ID：
   `conversation_events.id = FLOOR + legacy_event_id`。
3. 切写后所有新 `conversation_events.id`（含 Group）必须 `>= FLOOR`；回填开始前必须一次性把 `sqlite_sequence` 预留到 `FLOOR + max(legacy_event_id)`，使任何后续 AUTOINCREMENT（包括 Group 新消息）都高于所有潜在回填 ID。回填/切写期间必须停写；如果源表又出现更大的 legacy ID，脚本中止并重新预留，不能边回填边让 Group 新 ID 侵入 Direct 预留区间。
4. 因此裸整数引用可按区间判断：
   - `< FLOOR`：既有 `events` 或既有 Group `conversation_events` 的旧 ID；
   - `>= FLOOR`：统一后的 `conversation_events` ID。
   域仍由 `FactRef.kind` / `conversation_id` 校验，区间只做快速判别，不替代所有权检查。
5. `FLOOR + 既有最大 ID` 必须小于 JavaScript 安全整数上限 `9_007_199_254_740_991`，避免 Web/Android JSON 客户端丢精度。当前规模远低于此；脚本必须在开始前断言。

选择 10^15 而不是 2^62 的原因：浏览器与外部客户端用 JSON number 表示 event id，超过 2^53-1 会静默改值。

### 3.3 Direct conversation 身份

Direct 的客户端 `conversation_id` 是 WebUI 在 localStorage 生成的 UUID（`src/character_memory/web/app.js:117-122`），也可以是 API 调用方传入的任意字符串。它不是 Group 的 `conversations.id`。

冻结规范：

- 规范存储 ID：
  ```text
  direct-{character_id}-{sha256(character_id + "\0" + raw_conversation_id)[:32]}
  ```
  其中 `raw_conversation_id` 是原 `metadata.conversation_id`。
- 原始 `conversation_id` 继续保存在 `metadata_json.conversation_id`，保持 API/搜索/语音行为不变。
- 缺少或为空的 `metadata.conversation_id`：使用
  `direct-{character_id}-legacy-unscoped`。
  这是“旧数据没有会话标识”的每人物归档会话，不是新会话语义。需要用户确认是否接受这种归并。
- 同一 `(character_id, raw_conversation_id)` 永远映射到同一规范 ID；不同人物即使 raw 相同也不共享会话。

### 3.4 `conversations` / `conversation_members`

为每个规范 Direct conversation 建立：

```text
conversations:        id=规范ID, type='DIRECT', name=character_id
conversation_members: (规范ID, 'USER', 'user', 0)
                      (规范ID, 'CHARACTER', character_id, 1)
```

用途：

- Direct 历史按人物聚合时通过 `conversation_members(actor_type='CHARACTER', actor_id=?)` 找到该人物的全部 Direct conversation；
- Direct 事实的 owner 由成员关系与 `actor_id` 共同校验；
- 不依赖群成员授权路径，因为所有群读取都先 `get_group()`/`type='GROUP'`。

`joined_at` 使用该 conversation 最早迁移事实的时间；没有可用时间时使用迁移时间。`created_at`/`updated_at` 同理。迁移不写人物 Persona，也不要求人物当前 active；归档人物仍保留历史。

### 3.5 `turn_id`

`conversation_events.turn_id` 是 NOT NULL。Direct 现有数据没有 turn 概念，冻结派生规则：

回填：

```text
USER_MESSAGE:
  direct-turn-legacy-{legacy_event_id}
CHARACTER_MESSAGE:
  若 metadata.source_event_id 指向同人物、同规范会话的已迁移 USER_MESSAGE:
      direct-turn-legacy-{source_event_id}
  否则:
      direct-turn-legacy-{legacy_event_id}
```

切写后新事实：

```text
USER_MESSAGE:
  direct-turn-ce-{canonical_event_id}
CHARACTER_MESSAGE:
  有规范源事实: direct-turn-ce-{source_canonical_event_id}
  否则: direct-turn-ce-{own_canonical_event_id}
```

`legacy-` / `ce-` 前缀保证旧回填与新写入不会因 ID 空间不同而碰撞。Group 仍使用原有 `turn-<uuid>`。

## 4. 映射表

### 4.1 行级映射

源：`events` 中 `event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')` 的所有行。`event_time_epoch IS NULL` 的行不写入目标表，但必须进入 quarantine 与报告（4.3）。

目标：`conversation_events`。

| 旧字段 | 新字段 | 规则 | 不可映射时 |
|---|---|---|---|
| `events.id` | `conversation_events.id` | `FLOOR + events.id`；同时写 alias | 不存在，ID 必填 |
| `events.character_id` | `conversations.id` 的 owner + `conversation_members` + `actor_id` | Direct 事实所有者；角色消息 `actor_id=character_id`，用户消息 `actor_id='user'` | `character_id` 为空则整行进入 quarantine，不猜 |
| `metadata_json.conversation_id` | `conversation_events.conversation_id` | 规范 ID 公式（3.3）；原值保留在 metadata | 缺失 -> `legacy-unscoped` 会话并记 warning |
| — | `turn_id` | 3.5 规则 | 源事件不可解析则用自身 id |
| `metadata_json.channel` | 保留在 metadata | 期望 `'DIRECT'`；不从其他字段推断 | 缺失不阻止迁移 |
| `event_type` | `event_type` | 原值 `USER_MESSAGE` / `CHARACTER_MESSAGE` | 其他类型不在 W3 范围 |
| — | `actor_type` | `USER_MESSAGE -> 'USER'`；`CHARACTER_MESSAGE -> 'CHARACTER'` | 无 |
| — | `actor_id` | 用户 -> `'user'`；角色 -> `character_id` | 无 |
| `event_time` | `event_time` | **逐字复制原字符串**，不做时区/精度归一化 | 无 |
| `event_time_epoch` | `event_time_epoch` | **逐值复制**；不回算、不修复 | 为 NULL 的行不写入目标，进入 quarantine；旧读取本来就过滤它 |
| `content` | `content` | 逐字复制；不回填 `display_text`，不改写视觉/生活引用后缀 | 无 |
| `metadata_json` | `metadata_json` | 合法 JSON：语义等值对象复制；保留全部现有键 | 非法 JSON：quarantine 原文哈希，目标行写安全占位 metadata（见 4.3） |
| `metadata_json.display_text` | 保留 | 用户可见正文仍是 `display_text` 优先（`message_projection.py:73`） | 缺失则用 content |
| `metadata_json.source_event_id` | 保留 | 原值不改写；按 4.5 的 ID 区间解析 | 缺失/非法不阻止迁移 |
| `metadata_json.source_event_type` | 保留 | 原值 | 缺失不阻止 |
| `metadata_json.action`, `action_index` | 保留 | 可见动作类型与顺序 | 缺失不阻止 |
| `metadata_json.media_id` | 保留 | 原媒体引用；不复制二进制 | 资产缺失只记 warning，不删除事实 |
| `metadata_json.image_id`, `sticker_id` | 保留 | 图库/表情引用 | 资产缺失只记 warning |
| `metadata_json.voice_status`, `voice_media_id`, `voice_duration_ms`, `voice_error` | 保留 | 语音四键共享契约（`voice_message_fields.py:16-44`） | 缺失不阻止 |
| `metadata_json.live2d`, `life_reference`, `client_request_id`, `experience_scope`, `hidden` | 保留 | 原语义；`experience_scope` 不解释、不重写 | 非法 scope 按现有 `real_experience_sql` 规则保留；迁移不把它“修正”为 REAL |
| `metadata_json.sticker_label`, `image_label`, `media_name` | 保留 | 旧标签兼容字段 | 缺失不阻止 |
| `metadata_json.mentions` | 保留 | Direct 通常没有；不新增 | 无 |
| `metadata_json.trace` / `runtime_traces` | 不复制 | trace 仍是派生数据，不进入事实表 | 旧 trace 通过 alias 做 `has_trace` 解析 |
| `metadata_json.topic` 等其他键 | 保留 | 原样语义复制 | 无 |

### 4.2 参与者与媒体

- 参与者不新增副本：Direct conversation 的成员表是参与者 owner；每条事实的 `actor_type`/`actor_id` 是当条发言人。
- 用户身份在本项目是本地 `'user'`，与现有 Group 惯例一致（`group_conversation_service.py:132-134`）。
- `media_assets.character_id` 不变：Direct 仍是人物 ID，Group 仍是 `group:{conversation_id}`（`async_web.py:260-267`、`async_web.py:345-352`）。
- 生成的语音文件名 `voice-message-{event_id}`（`voice_message_materializer.py:88-103`）在切写后使用新 ID；新 ID 更长但仍在 `original_name` 的 180 字符限制内。

### 4.3 不可映射/异常行

| 情况 | 处理 | 理由 |
|---|---|---|
| `event_time_epoch IS NULL` | 不写入目标；quarantine `INVALID_TIME` | 现有历史/搜索/运行上下文都过滤 NULL epoch（`chat_history.py:38`、`message_search.py:38`）；迁移不应让旧隐藏行突然可见 |
| `metadata_json` 非法 | 目标行写入 `{"metadata_status":"INVALID","legacy_metadata_sha256":"..."}`；原串只存 quarantine 表 | Group `_event_from_row` 直接 `json.loads`（`group_store.py:224-236`），不能让非法 JSON 进入共享表；正文与时间仍可核对 |
| `character_id` 为空 | quarantine `MISSING_OWNER` | 无法建立 owner，不能猜 |
| `metadata.conversation_id` 缺失/空 | 归入 `legacy-unscoped` 会话并记 warning | 当前 Direct 历史本来按人物聚合，不按 conversation；这样不丢可见历史 |
| `media_id`/`image_id`/`sticker_id` 指向不存在的资产 | 照常迁移，记 warning `MISSING_MEDIA` | 现有投影已经会降级隐藏缺失资源；事实不能被删除 |
| 源行为 `ACTION`/`TIME_TICK`/`PROACTIVE_INTENT`/`VISUAL_OBSERVATION`/`LIFE_EVENT`/`DIARY`/`SOCIAL_POST`/`SPACE_*`/`WORLD_OBSERVATION` | 不迁移 | 非 W3 可见对话事实；由原 owner 保留 |
| 重复迁移同一源行 | alias 命中且哈希一致 -> no-op；不一致 -> 中止 | 幂等且不掩盖源变化 |
| `events.id`/既有 `conversation_events.id >= FLOOR` | 中止 | ID 区间契约被破坏，不能自动选新 floor |

### 4.4 旧 ID 的永久可解析性

新增迁移表（将来实施时创建，本轮不创建）：

```sql
CREATE TABLE conversation_fact_aliases(
    legacy_kind TEXT NOT NULL CHECK(legacy_kind='EVENT'),
    legacy_id INTEGER NOT NULL,
    character_id TEXT NOT NULL,
    legacy_conversation_id TEXT,          -- 原 metadata.conversation_id，可为空
    conversation_id TEXT NOT NULL,        -- 规范 Direct conversation_id
    conversation_event_id INTEGER UNIQUE, -- quarantine 时为 NULL
    content_sha256 TEXT NOT NULL,
    metadata_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('MIGRATED','QUARANTINED')),
    reason TEXT NOT NULL DEFAULT '',
    migrated_at TEXT NOT NULL,
    PRIMARY KEY(legacy_kind, legacy_id)
);

CREATE TABLE conversation_fact_migrations(
    migration_id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    last_legacy_id INTEGER NOT NULL DEFAULT 0,
    counts_json TEXT NOT NULL DEFAULT '{}',
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE conversation_fact_quarantine(
    legacy_kind TEXT NOT NULL CHECK(legacy_kind='EVENT'),
    legacy_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    raw_row_json TEXT NOT NULL,
    raw_metadata TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(legacy_kind, legacy_id)
);

-- 仅在执行回滚 inverse-delta 时创建/使用。
CREATE TABLE conversation_fact_reverse_aliases(
    conversation_event_id INTEGER PRIMARY KEY,
    legacy_event_id INTEGER NOT NULL UNIQUE,
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    metadata_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE conversation_fact_cutover(
    id INTEGER PRIMARY KEY CHECK(id=1),
    mode TEXT NOT NULL CHECK(mode IN ('LEGACY','SHADOW','UNIFIED')),
    migration_id TEXT NOT NULL,
    database_epoch TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

约束：

- `conversation_event_id` 与 `legacy_id` 双向唯一；同一源行不能映射到两个目标行。
- `status='QUARANTINED'` 时 `conversation_event_id` 为 NULL，但源行仍在 `events`，alias 保留原因。
- alias 是迁移账本，不是事实副本；它只保存 ID/哈希/原因，不保存正文或媒体。
- `conversation_fact_migrations` 的 `source_fingerprint` 是源数据库在迁移前的内容指纹（至少覆盖所有待迁移行的 id/content/event_time/metadata 规范化哈希）。
- `conversation_fact_cutover` 是单行状态；缺行时按 `LEGACY` 处理，禁止自动猜测为 UNIFIED。
- `content_sha256` 是 `content` 的 UTF-8 字节 SHA-256；`metadata_sha256` 是合法 JSON 以 `sort_keys=True, ensure_ascii=False, separators=(',',':')` 规范化后的 SHA-256；非法 JSON 用原串字节 SHA-256。
- **没有字段被静默丢弃**：不进入目标表的 trace/ACTION/观察等数据仍留在原 owner 表，并由 alias 或原 ID 可查；正文、时间、媒体引用只要源行存在就必须可在迁移报告中核对。

### 4.5 引用解析规则

统一读取器 `ConversationFactReader.resolve_direct(ref)`：

```text
ref.kind 必须是 EVENT
if ref.id < FLOOR:
    row = events.get(ref.id)
    require row.character_id == 期望 owner
    canonical_id = alias(ref.id).conversation_event_id（如有）
else if mode == 'LEGACY' and reverse_alias(ref.id) exists:
    row = events.get(reverse_alias(ref.id).legacy_event_id)
    require row.character_id == 期望 owner
else:
    row = conversation_events.get(ref.id)
    require conversation.type == 'DIRECT'
    require 期望 character_id 是该 conversation 的 CHARACTER member
```

Group 继续：

```text
ref.kind == CONVERSATION_EVENT
conversation_events.get(ref.id) where conversation_id == ref.conversation_id
require conversations.type == 'GROUP'
require 现有成员/trace 见证规则（W1）
```

裸整数字段的规则：

- `id < FLOOR`：旧 Direct Event 域；按旧表读。
- `id >= FLOOR`：统一后的 Direct/Group conversation event；必须结合 `FactRef.kind`、`conversation_id`、owner 校验，禁止只凭数字跨域读取。
- 任何错误/缺失/畸形引用返回 `UNRESOLVED`，不能回退到同号另一域（延续 W1 的 `fact_provenance.py:29-43` 行为）。

## 5. 兼容读取

### 5.1 模式

| 模式 | Direct 读 | Direct 写 | Group | 新表角色 |
|---|---|---|---|---|
| `LEGACY` | `events` | `events` | `conversation_events` | 不存在或仅空 schema |
| `SHADOW` | `events`（用户可见） | `events`（正式迁移期间应无写） | `conversation_events` | 只读对照/校验，不参与用户读取 |
| `UNIFIED` | `conversation_events` 的 Direct 域 | `conversation_events` | `conversation_events` | 唯一权威对话事实表 |

- 不采用双写。`SHADOW` 是隔离副本上的只读校验，不是正式运行模式。
- 正式切写必须在停写窗口内完成；不存在“旧写一半、新写一半”的用户可见阶段。
- 缺 `conversation_fact_cutover` 行时默认 `LEGACY`。`UNIFIED` 行存在但迁移账本/alias 不完整时，应用**fail closed**，不得自动回退或自动修复。

**迁移窗口内的“新旧同时可读”**：

- 回填期间应用停机，用户不可见任何中间态；旧读取仍只读 `events`。
- 校验/验收阶段可以同时打开两个只读查询：旧路径（`events`）与新路径（Direct 域 `conversation_events`），逐条对照，但**不得把两者合并成一份用户历史**；一旦合并，同一消息会出现两次。
- 切写后用户只走新路径；旧 ID 通过 alias 解析，旧表行保留。
- 读取器必须由 `conversation_fact_cutover.mode` 单一决定路径，不能在每个调用点各自判断“新表有没有数据”。

### 5.2 读取路由

| 路径 | 当前读取 | UNIFIED 后读取 | 关键约束 |
|---|---|---|---|
| `GET /v1/chat/history-page` | `ChatHistoryRepository.list_page`（`chat_history.py:34-59`） | `ConversationFactReader.list_direct_page`：join `conversation_members` 与 `conversations.type='DIRECT'` | 返回同一可见集合、顺序、分页游标；ID 用规范 ID |
| `GET /v1/chat/history` / 人物摘要 | `api.py:273-291`、`sqlite.list_chat_events`（`sqlite.py:1007-1010`） | 同上 | 摘要里的最新消息不能消失 |
| `GET /v1/search/messages` direct/global | `message_search.search_direct`（`message_search.py:32-69`） | Direct 域 `conversation_events` 查询 | 命中、顺序、`jump_before_id` 一致；不能旧新相加 |
| `GET /v1/groups/{id}/history` | `GroupRepository.list_event_page` | 不变 | Direct 行不得混入 Group 查询 |
| `/v1/events/stream` direct | `character_event` 的 payload id 来自 Direct event（`async_conversation.py:451-486`） | 规范 ID | SSE 通道/hub 仍是 ephemeral；重连由历史对账（`async_conversation.py:35-40`） |
| `/v1/events/stream` group | `group_character_event` | 不变 | 新 Group ID 也在 FLOOR 之上，但 `conversation_id` 不变 |
| Direct VOICE_MESSAGE | `voice_message_service._apply` 读/写 `events`（`voice_message_service.py:45-69`） | `ConversationFactWriter.update_metadata` 读/写 Direct 行 | 必须同时校验 character + raw `metadata.conversation_id`，防止同号写错行 |
| Group VOICE_MESSAGE | `voice_message_materializer.materialize_group`（`voice_message_materializer.py:153-201`） | 不变 | 继续用 `GroupRepository.update_event_metadata` |
| 运行时最近上下文 | `store.list_events`（`person_context.py:50-57`）与 `_last_chat_before`（`person_runtime.py:157-166`） | Direct 对话事实来自新表；非对话事件仍来自 `events` | 必须排除旧 `events` 中已迁移消息，避免重复；不能简单两表相加 |
| Memory provenance | `fact_provenance.resolve_memory_provenance`（`fact_provenance.py:9-44`） | `ConversationFactReader.resolve_direct` + Group 原路径 | 旧 Memory 的 `source_event_id` 仍可解析；新 Memory 用规范 ID |
| Reply suggestions direct | `application/reply_suggestions.py:50-59` 直接查 `events` | Direct 域新表 + 成员校验 | `ref.kind='EVENT'` 不能误入 Group 分支 |
| 生活/关系/头像/空间媒体 | `character_life.py:288`、`character_relationships.py:30-47`、`avatar_web.py:79`、`space_media_executor.py:320-323` | Direct 对话事实用新表；非对话仍用旧表 | 所有“人物全部消息”聚合必须去重 |
| 计数/归档 | `sqlite.count_character_messages`（`sqlite.py:1012-1026`） | 只数规范 Direct 行 | 不能旧表 + 新表，否则迁移消息双计 |
| 收据/投递/调度恢复 | `message_receipts.py:43-66`（`EVENT` 分支用 `store.get_event` 校验来源）、`reaction_delivery.py` | 旧 ref 通过 alias 解析；新 ref 用规范 ID 经 `ConversationFactReader` 校验 | `EVENT` 收据的 `source_ref_json` 在切写后会指向 `conversation_events`；只改 SQL 表名或只加 alias 查询都会漏，必须统一走 reader。切写前必须清空/阻断旧协议 PENDING/RUNNING |
| Intent 源校验 | `sqlite.py:1086-1112` 的 `EXISTS (SELECT 1 FROM events e WHERE e.id=intents.source_event_id)`；`chat_service.resolve_real_intent_source`（`chat_service.py:31-48`） | 改用 `ConversationFactReader.resolve_direct` | 迁移后的用户消息源不再是 `events` 行；SQL 直连会误判 `SOURCE_MISSING` 并错误 SUPPRESS |
| 生图任务/通话邀请 | `image_generation_store.py:43-52`、`call_invitations.py:66` | 新源 ID >= FLOOR；旧源 ID < FLOOR | 唯一键/`UNIQUE` 约束仍能防重；不得把旧/新 ID 互换 |
| Memory 运营指标 | `sqlite.py:974-983` 的 `LEFT JOIN events e ON e.id=m.source_event_id` | 先按区间分流，再取 Direct 域事实或非对话事件 | 否则迁移后的 Direct Memory 来源在指标里变成 `OTHER` |
| 关系/生活原始 JOIN | `character_relationships.py:79` 的 `LEFT JOIN events e ON e.id=i.source_event_id` | 按区间 + `ConversationFactReader` 解析 | 不能把 `id >= FLOOR` 直接 JOIN 旧表 |
| 调度水位 | `async_conversation.py:424-445` 的 `_latest_direct_user_id` / `_latest_direct_visual_source_id` → `store.latest_direct_event_id`（`sqlite.py:915-939`，按 metadata `conversation_id`） | 按原始 `conversation_id` 查 Direct conversation 的新表水位；`latest_direct_event_id` 改成区间感知 | 否则切写后看不到新用户事实，旧事实仍被当成最新，generation 永远 SUPERSEDED 或错误回复旧消息 |
| 生活引用续聊 | `life_conversation.py:104-111` 在缺少 conversation_id 时回查旧 `events` 最新用户消息 | 改为 `ConversationFactReader` 的 Direct 最新会话 | 否则迁移后旧表可能已无新消息，续聊入口返回 409 |
| 人物摘要最新角色消息 | `api.py:285-293` 的 `list_events(event_type=CHARACTER_MESSAGE)` | Direct 域取最新角色消息；`latest_message` 走 `list_chat_events` 的替代查询 | 否则侧栏摘要/最新回复 ID 停在切写前 |
| 主动派发等待判断 | `application/proactive_service.py:67-75` 的 `list_chat_events(limit=1)` | 同上 | 否则主动派发可能误判“用户已回复”，或在迁移后看不到最新角色消息 |
| 最近聊天/时间间隔 | `person_runtime.py:157-166` 的 `list_events` | Direct 域 + 非对话事件合并去重 | 否则“上次聊天”永远停在切写前 |
| Direct 生图任务/通话邀请（建表） | `image_generation_store.py:11-12`、`call_invitations.py:30` | 新源 ID >= FLOOR；旧行 < FLOOR | 同号不再冲突；按区间 + owner 校验 |
| 历史 `has_trace` | `history_web.py:90-96`、`chat_history.trace_sources` | 旧 trace 通过 alias 映射到规范 ID | 迁移消息的 trace 状态不能丢 |

Direct 读取器必须把 `conversation_events` 行适配为 domain `Event`（`character_id` 从 Direct conversation 的 CHARACTER member 取，`event_type` 转回 `EventType`），再复用 `project_direct_message`（`message_projection.py:55-116`）；Group 仍用 `project_group_message`。不得把 Direct 行直接塞进 `GroupEvent` 或群投影，否则 `character_id`/`role`/`source_event_id` 语义会漂移。

### 5.3 共享表内的 Direct 行必须被所有 Group 查询排除

迁移后 `conversation_events` 同时含 DIRECT 与 GROUP 行。现有若干 Group 查询只按 `conversation_id`/`actor_id` 过滤、不校验 `conversations.type='GROUP'`，会把 Direct 事实误当群事实。实施时必须逐条加 `type='GROUP'` 或改走 Direct reader：

| 位置 | 现状 | 风险 |
|---|---|---|
| `storage/message_search.py:83-85` | `conversation_events e JOIN conversations c`，只过滤 `c.archived_at IS NULL` | global 搜索会把 Direct 事实标成 `scope='GROUP'`；必须加 `c.type='GROUP'` |
| `group_store.py:871-884` `count_character_messages` | 只按 `actor_id/actor_type` 统计全表 | Direct 角色消息会被当成群消息重复计数；必须 join `conversations.type='GROUP'` |
| `group_store.py:922-938` `has_recent_character_activity` | 只按 `actor_id/actor_type/event_time` 查全表 | Direct 活动会被当成群活动；必须 join `conversations.type='GROUP'` |
| `character_life.py:290` | Group source 扫描全 `conversation_events`，参与谓词含成员/trace | Direct 事实可能以 `group` 来源重复出现；必须加 `conversations.type='GROUP'` | |
| `character_relationships.py:45-50` | Group 互动查询依赖成员表 | Direct 规范会话也有成员行，可能被投影成群互动；必须加 `type='GROUP'` |
| `life_conversation.py:16-29,77-91` | `kind='group'` 分支只查组成员与 trace | Direct 事实不会走该分支的唯一保证是 source key 域；迁移后必须显式校验会话 `type='GROUP'` |
| `group_autonomous_visual.py:19-25`、`group_scenes.py:28-29,53-54` | 按 `conversation_id` 查 `conversation_events` | 调用方只传 group id 时安全，但应加注释/断言，防止未来传错 |
| `group_store.py:550-564,608-630,834-884` | 按 `conversation_id` 查询 | 同上；新增 Direct 行不会混入，因为规范 Direct `conversation_id` 与 Group id 不重叠 |
| `storage/message_receipts.py:56-58` | 只按 `id + conversation_id` 校验 Group 事实 | 若 ref.kind 误标 `CONVERSATION_EVENT` 且传入 Direct conversation_id，会通过；必须再校验 `type='GROUP'` |

这张表是 W3 实施的最低兼容清单；任何新增的 `conversation_events` 全表/跨会话查询都必须显式处理 `type`。

### 5.4 搜索/历史/SSE/语音的联合一致性

- **搜索**：Direct 命中集合必须与 UNIFIED 历史中可见消息一致；`jump_before_id` 返回规范 ID，能打开对应历史页。
- **历史**：`before_id` 游标使用 `(event_time_epoch, id)`；Direct 聚合跨多个规范 conversation 时仍按该顺序稳定分页。
- **SSE**：payload 的 `id` 是规范 event ID；hub 的 `id:` 仍是进程内递增序号，二者不能混用。新连接不重放旧 transient 事件，由历史对账补齐。
- **语音**：Direct VOICE_MESSAGE 的 `voice_media_id` 只能更新同一规范事实；Group 继续走 group repo。失败时保留文本（`voice_message_fields.py:27-37`）。

## 6. 唯一写入口

### 6.1 `ConversationFactWriter`

将来实施时新增一个应用/存储边界（名称可调整，语义冻结）：

```text
ConversationFactWriter
├─ append_direct(event, *, owner_character_id, raw_conversation_id)
├─ append_group(event)                  # 委托现有 GroupRepository
├─ update_metadata(ref, metadata)       # Direct/Group 语音等元数据更新
└─ ensure_direct_conversation(...)      # 事务内创建 conversations/members
```

规则：

- `LEGACY`：`append_direct` 委托 `SQLiteStore.append_event`，行为与今天一致。
- `UNIFIED`：`append_direct` 在一个 `BEGIN IMMEDIATE` 事务内：
  1. 确保规范 Direct conversation 与成员存在；
  2. 插入 `conversation_events`，ID 由 AUTOINCREMENT 产生且 `>= FLOOR`；
  3. 更新 `conversations.updated_at/updated_at_epoch`；
  4. 返回带规范 ID 的 `Event`；
  5. 调用方仍可在同一事务内写 acceptance receipt / delivery source。
- Group 可见消息继续委托 `GroupRepository.append_event`，但应用层必须经 writer；hidden control facts（`GROUP_OPPORTUNITY`、`SCENE_DAY_ADVANCED`）可保留 GroupRepository 内部调用。
- 所有 Direct 可见消息写入点必须改到 writer：`async_web.py:292-299`、`person_runtime.py:787-795`、`visual_runtime.py:214-235`、`visual_capture_web.py:270-284`、`api.py:285-293` 的摘要读取、`proactive_service.py:67-75`。直接调用 `store.append_event(USER_MESSAGE/CHARACTER_MESSAGE)` 在 UNIFIED 下必须失败。
- `role_collaboration.py:109-140` 的 Group 共享消息与 ACTION 投影保持原 owner：Group 消息继续 `GroupRepository.append_event`，ACTION 投影继续 `store.append_event(ACTION)`；它们不是 Direct 对话事实，不进入 W3 映射。
- `eval/runner.py:55-60,77-84` 的测试 fixture 可以继续直接写 `events`；生产配置不得把它当作写入口。CI 允许清单必须按文件而非按 `event_type` 放行。

### 6.2 防止旁路写入

三层防线：

1. **代码守卫**：`SQLiteStore.append_event` 在 `mode=UNIFIED` 且 `event_type in {USER_MESSAGE, CHARACTER_MESSAGE}` 时抛 `LegacyDirectWriteClosed`，不静默写旧表。
2. **数据库守卫**（显式切写时安装，代码合并不安装）：
   ```sql
   CREATE TRIGGER events_no_direct_message_after_cutover
   BEFORE INSERT ON events
   WHEN (SELECT mode FROM conversation_fact_cutover WHERE id=1)='UNIFIED'
     AND NEW.event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')
   BEGIN SELECT RAISE(ABORT, 'direct conversation facts must use ConversationFactWriter'); END;

   CREATE TRIGGER conversation_events_require_domain
   BEFORE INSERT ON conversation_events
   WHEN NEW.event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')
     AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id=NEW.conversation_id AND c.type IN ('DIRECT','GROUP'))
   BEGIN SELECT RAISE(ABORT, 'conversation fact requires a DIRECT/GROUP conversation'); END;
   ```
   触发器是最后防线，不是唯一防线；脚本/迁移工具可在显式维护窗口临时绕过。
3. **CI/静态检查**：禁止 `src/` 中新增 `INSERT INTO events` / `INSERT INTO conversation_events`，除非文件在允许清单（迁移脚本、底层 storage、测试 fixture）。现有写入点已可枚举（第 1.2 节）；新增直写必须失败。

`GroupRepository.append_event` 保留为 Group 领域底层原语；应用层可见消息统一经 writer。若未来需要更强约束，可把 Group 可见消息写入也包进 writer 的 `append_group`，但在本 ADR 中不改 Group 行为。

## 7. 回填方案

### 7.1 隔离副本

1. **只读盘点**：在正式库上用只读连接统计：
   - `events` 按 `event_type` 计数、`id` 范围、`event_time_epoch IS NULL` 数量；
   - `metadata_json` 非法数量、缺失 `conversation_id` 数量、缺失媒体引用数量；
   - 既有 `conversation_events`/`conversations` 计数与最大 ID；
   - `schema_migrations` 版本；
   - `reaction_delivery_sources`、`message_acceptance_receipts` 中旧 Direct ref 的 pending 状态。
2. **backup**：使用 SQLite online backup API（不是 `cp` 活库文件/WAL）生成带时间戳的副本；打开副本执行 `PRAGMA integrity_check`、`PRAGMA foreign_key_check`，核对计数与 schema。
3. **隔离迁移**：只在副本上运行迁移脚本。所有连接使用副本路径；不得指向正式库。
4. **隔离验收**：在副本上启动临时 API/临时配置，按第 11 节检查历史、搜索、SSE、语音、Memory、收据/投递。

### 7.2 迁移算法

- 选择：`event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')` 的全部行，按 `events.id` 升序。`event_time_epoch IS NULL` 的行只写 alias/quarantine，不写目标表。
- 每批（建议 200–1000 行）一个事务：
  1. 读取源行；
  2. 计算规范 conversation ID、turn ID、目标 ID、hash；
  3. `INSERT`/`ON CONFLICT` 写 `conversations`、`conversation_members`；
  4. 写 `conversation_events`（目标 ID = FLOOR + legacy_id）；
  5. 写 alias（含 status/reason）；
  6. 更新 migration checkpoint `last_legacy_id` 与 counts；
  7. 提交。
- 先按 legacy id 顺序插入，保持 Direct 插入顺序；历史读取仍按 `(event_time_epoch,id)`。
- 迁移事务内不得调用模型、网络、Embedding 或媒体读取；媒体只做引用存在性检查。
- 迁移不修改源 `events` 行的任何列。
- 迁移开始时读取 `max(events.id)`，先把 `conversation_events` 的 `sqlite_sequence` 预留到 `FLOOR + max_legacy_id`，再写任何目标行；否则 Group 的 AUTOINCREMENT 可能先占用将来要用的 Direct 回填 ID。
- 目标 `conversation_events.id` 用显式值（FLOOR + legacy_id），并保持 `sqlite_sequence` 不低于预留值；不要让 AUTOINCREMENT 自行分配 Direct 回填 ID。
- 如果迁移过程中源表出现新的、大于初始 `max_legacy_id` 的 Direct 消息行，脚本中止；重新停写、重新预留后再继续。

### 7.3 幂等

- alias 主键 `(legacy_kind, legacy_id)` 保证同一源行最多一次。
- 已存在 alias：
  - `content_sha256`/`metadata_sha256`/canonical conversation 全部一致 -> skip；
  - 任一不一致 -> 中止并报告，不覆盖。
- 重跑同一 `migration_id`：目标行已存在则校验后 skip；最终计数/哈希不变。
- 不同 `migration_id` 对已迁移库：拒绝，除非显式 `--force-new-migration` 且用户批准（默认不提供）。
- 空库/无待迁移行：合法 no-op，写 migration 记录或直接报告 0，不改变模式。

### 7.4 中断恢复

- checkpoint 只在批次事务提交后更新；崩溃回滚当前批次。
- 重启脚本从 `last_legacy_id` 继续；已提交批次不会重复。
- 任意时刻杀进程后重跑，最终 alias/target/checksum 必须与一次跑完一致。
- 若崩溃发生在 `mode=UNIFIED` 写入之前，仍可安全重跑；若发生在切写之后，走第 8 节回滚/恢复，不自动“猜”迁移状态。

### 7.5 切换

正式切换必须在停写窗口：

1. 停止 Character Runtime、Dev/Space/World/Group workers 与所有可能写 `events`/`conversation_events` 的进程；停止前必须取得用户许可。
2. 再次 backup；确认没有 `PENDING` 旧 Direct delivery、没有运行中的旧 reaction preparation、没有未完成的 acceptance dispatch。
3. 在正式库运行与副本同一 `migration_id` 的迁移脚本；脚本必须校验源指纹一致，否则中止。
4. 校验通过后，在同一事务写 `conversation_fact_cutover(mode='UNIFIED', migration_id=...)`，安装第 6.2 节触发器。
5. 启动新代码；执行只读 smoke（历史/搜索/语音/SSE）并记录证据。
6. 失败则立即执行第 8 节回滚，不继续放量。

## 8. 回滚方案

### 8.1 触发条件

- 历史/搜索出现缺行、重复行或跨人物泄露；
- Direct 消息正文/时间/参与者/媒体与迁移前哈希不一致；
- 语音更新写错行或群/单聊通道串线；
- Memory provenance 或收据/投递解析失败；
- `PRAGMA integrity_check`、外键检查或迁移账本不一致；
- 用户明确要求停止。

### 8.2 回滚步骤

1. 停止所有写入进程（需要用户许可）。
2. 记录当前 `conversation_fact_cutover`、计数、哈希、失败证据；备份当前库。
3. 判断是否已经产生切写后的 Direct 事实：
   - **无新事实**：直接把 `mode` 置回 `LEGACY`，删除/禁用触发器，启动 W3 代码的 LEGACY 模式（或恢复切写前的旧二进制）。旧 `events` 行从未被修改，完整可读。
   - **有新事实**：执行 inverse-delta：
     - 对每个 `id >= FLOOR` 且属于 Direct 域的 conversation fact，插入一条新的 `events` 行（旧表 AUTOINCREMENT 分配新 ID），写 reverse alias；
     - 保留新表行与 alias，不删除；
     - 校验 reverse alias 覆盖所有切写后 Direct 事实；
     - 置 `LEGACY` 并启动 **W3 代码的 LEGACY 模式**；`ConversationFactReader` 通过 reverse alias 解析仍在系统中的规范 ref。
4. 校验旧数据完整性：对照迁移前 backup 的 `events` 消息行计数/哈希，必须完全一致（迁移是 copy-only）。
5. 保留迁移表、alias、失败报告和 backup，供复审与再次尝试；不得自动删除。

如果要回滚到**切写前的旧二进制**而不是 W3 代码的 LEGACY 模式，则必须先把所有裸整数引用（receipts、delivery、call_invitations、生图任务等）改写为 inverse-delta 后的旧 ID；否则旧二进制只认识小 ID，可能拒绝或误解规范 ref。做不到时只能恢复切写前 backup，并明确接受切写后事实丢失。

### 8.3 回滚后的数据完整性

- **迁移前旧数据**：完整。源 `events` 消息行从未 UPDATE/DELETE；`media_assets` 未改；Group 表未改。
- **切写后新数据**：只有执行 inverse-delta 才能保证旧代码/旧读取路径可见；否则只能恢复到切写前 backup，并明确接受丢失切写后新事实。是否接受该损失必须由用户拍板（第 12 节）。
- **不能把“有 backup”当作无损回滚**：backup 恢复会丢失 backup 之后的新事实；ADR 要求优先使用“旧表保留 + inverse-delta + LEGACY 模式”的回滚路径。

## 9. 正式操作边界

### 9.1 代码合并不会触发迁移

冻结：

- 应用启动、`SQLiteStore._init_schema`、`GroupRepository._init_schema`、CI、pytest、Docker/dev stack **不得**扫描 `events` 消息行、不得回填、不得切换模式。
- 代码合并只能新增：读取器、writer、迁移脚本、迁移表空 schema（如需要）、测试和文档。
- 缺少 cutover 行时默认 `LEGACY`；不得根据“发现新表为空”自动推断并执行迁移。
- `mode=UNIFIED` 但 migration/alias 不完整时启动失败并给出明确错误；不得自动回退到 LEGACY（会掩盖半迁移状态）。

### 9.2 谁、在什么条件下执行

正式迁移只能由**用户明确授权的操作者**执行：

1. W3 ADR 经用户批准；
2. W2 已合并且 W3 依赖的收据/投递边界稳定；
3. 第 7.1 节隔离副本 dry-run 与第 11 节验收通过，证据已提交 Issue/PR；
4. 迁移前 backup 已完成并验证；
5. 维护窗口已确认，且用户已明确许可停止服务；
6. 没有进行中的聊天/生成/投递；旧协议 pending 已清空或阻断；
7. 回滚路径已在副本上演练。

执行命令（示例，不在本轮执行）：

```bash
python scripts/migrate_conversation_facts.py \
  --db <isolated-or-production.db> \
  --backup <timestamped-backup.db> \
  --migration-id <id> \
  --apply \
  --confirm <source-fingerprint>
```

脚本必须：

- 拒绝活跃 WAL 的裸文件复制；
- 拒绝 `max(events.id, conversation_events.id) >= FLOOR`；
- 拒绝没有 backup 的 `--apply`；
- 拒绝源指纹与 dry-run 不一致；
- 默认不提供自动 mode 切换；`--cutover` 必须是独立显式步骤。

### 9.3 授权边界

- PR 合并、CI 通过、Issue 关闭都不等于批准正式迁移。
- 任何 agent 不得在未获得当轮用户明确指示时停止正式服务或运行 `--apply`/`--cutover`。
- 正式迁移的批准记录必须包含：操作者、数据库路径、backup 路径、migration_id、dry-run 证据、维护窗口、回滚负责人。

## 10. 本轮交付边界

- 本轮 PR 只新增本文档与文档索引；无 `src/`、`tests/`、schema、数据库变更。
- 不包含 W3 实现脚本；不包含 W2 行为修改。
- 不改变当前 `main` 的任何运行行为。
- 文中“将来实施”的表名/文件名是契约候选；实际实现可在不改变语义的前提下调整命名，但必须保持本文的 ID、映射、模式、唯一写入口、回填和回滚不变。

## 11. 验收清单

迁移实施（不是本轮）必须全部满足；任何未执行/skip 不得记为 PASS。

### 11.1 数据

- [ ] 源/目标计数：`USER_MESSAGE` + `CHARACTER_MESSAGE` 的**全部**源行都有 alias；`MIGRATED + QUARANTINED` 等于源行数，且每一行有原因；不迁移的 `event_type` 在报告中单列。
- [ ] ID：每个 MIGRATED 行 `conversation_event_id == FLOOR + legacy_id`；所有新 ID `< 9_007_199_254_740_991`；无重复/冲突；由 alias 反查能唯一回到旧 ID。
- [ ] 历史 ID 可核对：任意抽样旧 `events.id` 通过 alias 得到规范 ID，再按规范 ID 读回同一正文/时间/参与者；反向通过 `conversation_fact_reverse_aliases`（仅在回滚后存在）也能回到旧表行。
- [ ] 正文：`content` 逐字一致；用户可见 `display_text` 语义一致。
- [ ] 时间：`event_time` 字符串与 `event_time_epoch` 逐值一致；NULL epoch 行按 quarantine 处理，未发生可见性变化。
- [ ] 参与者：Direct 规范会话有且仅有 `USER/user` 与 `CHARACTER/<character_id>`；每条消息 `actor_type/actor_id` 正确；无跨人物混入。
- [ ] 媒体：`media_id`/`image_id`/`sticker_id`/语音四键原样保留；缺失资产有 warning，事实未丢。
- [ ] 旧表：迁移前后 `events` 中两类消息行的计数/内容哈希完全一致；无 UPDATE/DELETE。
- [ ] `PRAGMA integrity_check` 与 `foreign_key_check` 通过。

### 11.2 行为

- [ ] Direct 历史：迁移前后可见消息集合、顺序、分页游标、正文、媒体、参与者、时间一致；ID 改为规范 ID。
- [ ] Direct 搜索：命中集合/顺序一致；`jump_before_id` 可打开对应历史；global 搜索不重复、不漏。
- [ ] Direct SSE：新消息 payload 使用规范 ID；通道仍为 `direct:{character}:{raw_conversation_id}`；历史对账可补齐遗漏。
- [ ] Direct 语音：pending/ready/failed 状态更新同一规范事实；不会因同号写到 Group 或另一 Direct 会话；Group 语音不变。
- [ ] Memory provenance：旧 `source_event_id` 解析到规范事实；新 Memory 用规范 ID；Direct/Group 同号不串。
- [ ] 收据/投递：重复 `request_id`、重启恢复、COMMITTED/UNKNOWN 语义不变；不重复事实/模型调用。
- [ ] 计数/归档/生活/关系/建议：不双计、不丢迁移消息；非对话事件仍可见。
- [ ] 会话隔离：Direct 规范会话不出现在 `/v1/groups`、群归档、群成员接口；`message_receipts.py:56-58` 的 Group 校验对 Direct conversation_id 返回失败。
- [ ] 全表查询：第 5.3 节列出的每条 Group/全局查询都有 `type='GROUP'` 或改走 Direct reader；新增的全表查询不会把 Direct 行计入群统计/群搜索/群生活记录。

### 11.3 幂等与恢复

- [ ] 同一迁移重复执行：0 新增 target/alias，最终计数/哈希不变。
- [ ] 在每类批次边界杀进程：重跑后最终结果与一次跑完一致。
- [ ] 空库/仅 Group 库/旧 schema 库：no-op 或明确拒绝，不产生半迁移状态。
- [ ] `mode=UNIFIED` 但账本不完整：启动 fail closed，不自动修复。

### 11.4 回滚

- [ ] 无切写后新事实：切回 LEGACY 后历史/搜索/语音/记忆与迁移前一致，旧数据哈希不变。
- [ ] 有切写后新事实：inverse-delta 后旧表可见新事实，reverse alias 完整；不删除新表/alias。
- [ ] 回滚演练在隔离副本完成并留存证据。

### 11.5 操作

- [ ] 合并后应用启动不触发迁移；默认 LEGACY。
- [ ] 正式迁移有用户书面批准、backup、维护窗口、dry-run 证据和回滚负责人。
- [ ] 未运行/失败/skip 的项明确标 BLOCKED，不冒充 PASS。

## 12. 需要用户拍板

以下问题会改变数据所有权或产品行为，ADR 不自行决定：

1. **迁移边界**：只迁移 `USER_MESSAGE`/`CHARACTER_MESSAGE`（本文推荐），还是也迁移 `VISUAL_OBSERVATION`/`TIME_TICK`/`PROACTIVE_INTENT` 等 Direct 源事件？
   - 推荐：只迁移可见对话事实；其余保留原 owner，避免 W3 膨胀。
2. **旧表处置**：`events` 中的消息行是否永久保留为只读归档（推荐），还是在某个保留期后删除？
   - 删除会影响回滚、旧引用解析和历史审计，必须单独批准。
3. **Direct 会话身份**：是否接受“每个 `(character_id, raw_conversation_id)` 一个 DIRECT conversation，历史按人物跨会话聚合”（推荐）？缺少 `conversation_id` 的旧消息是否归入 `legacy-unscoped`（推荐），还是 quarantine 不展示？
4. **切写窗口**：是否接受正式迁移必须停写（推荐），还是要求在线双写/零停机？双写会产生两个事实源，需要额外 ADR。
5. **回滚深度**：回滚是否必须保留切写后新事实（inverse-delta，推荐）？还是只保证迁移前旧数据、切写后数据从 backup 恢复并接受丢失？
6. **ID 契约**：是否接受 `FLOOR=10^15` 的全局 ID 区间契约（推荐，兼容 JS 安全整数）？外部 Android/客户端若持有旧数字 ID，需要按 alias 解析。
7. **正式操作授权**：由谁执行、何时执行、维护窗口多长、dry-run 证据由谁验收？本文推荐：用户本人或明确指派的操作者，必须在 Issue/PR 留下书面批准；合并/CI 不构成批准。

## 13. 实施拆分（将来，不在本轮）

1. **W3-a 读取抽象**：`ConversationFactReader` + alias/模式表 + 单测；不写数据、不切模式。
2. **W3-b 写入抽象**：`ConversationFactWriter` + LEGACY 等价测试 + bypass guard；默认 LEGACY。
3. **W3-c 迁移工具**：隔离副本 dry-run、幂等、checkpoint、崩溃注入、校验报告；不接触正式库。
4. **W3-d 双读验收**：在副本上让历史/搜索/SSE/语音/Memory/收据全部走新读取器，与旧路径做对照。
5. **W3-e 正式切写**：独立操作步骤，用户批准后执行；代码合并不触发。

## 14. 源码索引

- 事实表：`src/character_memory/storage/sqlite.py:270-336`、`src/character_memory/group_store.py:170-210`
- Direct 写入：`src/character_memory/application/chat_service.py:62-80`、`src/character_memory/async_web.py:292-299`、`src/character_memory/runtime/person_runtime.py:787-795`
- Group 写入：`src/character_memory/application/group_conversation_service.py:118-141`、`:627-640`、`src/character_memory/group_store.py:477-500`
- 历史/搜索：`src/character_memory/storage/chat_history.py:34-59`、`src/character_memory/history_web.py:85-117`、`src/character_memory/storage/message_search.py:32-119`
- 运行上下文：`src/character_memory/runtime/person_context.py:50-57`、`src/character_memory/runtime/person_runtime.py:157-166`
- 声音：`src/character_memory/application/voice_message_service.py:45-69`、`src/character_memory/application/voice_message_materializer.py:117-201`
- 引用/收据/投递：`src/character_memory/domain/facts.py:9-23`、`src/character_memory/application/fact_provenance.py:9-44`、`src/character_memory/storage/message_receipts.py:30-66`、`src/character_memory/storage/reaction_delivery.py:27-34`
- 所有权文档：`docs/current/MEMORY.md:5-17`、`docs/current/CONVERSATION_RUNTIME.md`

## 15. 实施记录（UNIFIED，正式切换未执行）

本节记录与第 1–14 节冻结设计的差异与实现落点。设计的 ID 区间、映射、唯一写入口、回填与回滚边界未改变；变化只在“模式”与“回滚深度”两处，均由用户取舍拍板。

### 15.1 单一模式

- 移除 `LEGACY` / `SHADOW` / `UNIFIED` 的运行时分叉与 `conversation_fact_cutover` 开关驱动的读写路由。
- `conversation_fact_cutover` 表保留（空 schema），只用于记录正式切换审计；应用启动与读取路径不查询它。
- 唯一实现：`src/character_memory/storage/conversation_facts.py`
  - `ConversationFactWriter.append_direct / update_direct_metadata / append_group`
  - `ConversationFactReader.resolve_direct / list_page / list_chat_events / search / jump_before / latest_fact_id / newest_direct_input`
  - `ConversationFactSchema` 只建空表与预留 `sqlite_sequence`，不扫描、不回填。
- 运行时守护：`SQLiteStore.append_event` 对 `USER_MESSAGE` / `CHARACTER_MESSAGE` 抛 `LegacyDirectWriteClosed`；迁移与测试夹具使用显式的 `append_legacy_event`。
- CI 守护：`scripts/check_conversation_fact_writes.py` 拒绝允许清单之外的新 `INSERT INTO events` / `INSERT INTO conversation_events`。

### 15.2 旧表处置

`events` 中的消息行保留且永不改写（迁移是 copy-only）。产品代码不再读写它们；历史旧 ID 通过 `conversation_fact_aliases` 解析到规范行。旧行只在迁移前的 backup 与审计中可见。若日后要删除，必须按第 2 节边界单独批准。

### 15.3 回滚取舍（简化 inverse-delta）

用户拍板：**回滚以备份恢复为主**，不追求保留切写后新事实的完整 inverse-delta。因此：

- 第 8 节的 `conversation_fact_reverse_aliases` 表与逐行反向插入流程**不实现**。
- 回滚步骤：停写 → 记录当前计数与哈希 → 用切换前 online backup 恢复整库 → 以切换前二进制启动。
- 代价明示：backup 之后产生的 Direct 新事实会随恢复丢失。若不能接受该损失，只能在切换窗口内把新事实导出为 JSONL 并人工回放，本实现不提供自动回放。
- `conversation_fact_aliases` / `conversation_fact_migrations` / `conversation_fact_quarantine` 与 backup 一同保留，供复审。
- 回滚后旧 `events` 行完整可读，因为迁移从未 UPDATE/DELETE 它们。

### 15.4 5.3 硬前置的落点

| 前置 | 实现位置 |
|---|---|
| Group/全局查询补 `type='GROUP'` | `storage/message_search.py::search_group`、`group_store.py::count_character_messages` / `has_character_activity_since`、`character_life.py` 的 group source、`character_relationships.py::_group_event_locked`、`life_conversation.py` 的 group 分支、`storage/message_receipts.py` 的 `CONVERSATION_EVENT` 校验 |
| `intents.source_event_id` JOIN 改走新事实表 | `storage/sqlite.py::suppress_non_direct_intents`（按 ID 区间 UNION 两个域） |
| `memories` 指标改走新事实表 | `storage/sqlite.py::memory_operational_metrics`（按区间 LEFT JOIN 两个域） |
| `character_relationships` evidence 改走新事实表 | `character_relationships.py::_direct_source_row_locked` |
| `message_receipts` EVENT 分支改走新事实表 | `storage/message_receipts.py::accept` 使用 `ConversationFactReader.resolve_direct` |

### 15.5 迁移脚本

`scripts/migrate_conversation_facts.py`：

- 默认 `--dry-run`，只读不写；输出源指纹与前后计数。
- `--apply` 必须有 `--backup`（online backup API + `integrity_check` + `foreign_key_check`），或对一次性副本显式 `--allow-unbacked`。
- alias 主键幂等；哈希或规范 ID 不一致即中止，不覆盖。
- 每批一个 `BEGIN IMMEDIATE`，checkpoint 只在提交后推进；中途被杀可续跑。
- 迁移前把 `conversation_events` 的 `sqlite_sequence` 预留到 `FLOOR + max(legacy_id)`；迁移中出现更大的源 ID 立即中止。
- `--cutover` 是独立显式步骤，写 `conversation_fact_cutover(mode='UNIFIED')`；本 PR 未执行。
- `--confirm <fingerprint>` 拒绝源库与 dry-run 不一致的 apply。

### 15.6 验收证据边界

本 PR 的副本验收：真实 `data/character-memory.db` 的只读 online backup 副本上 dry-run + apply，逐行核对正文/时间/参与者/媒体，重复执行无副作用，批间中断可续跑且终态与一次跑完一致。**未**对真实库执行迁移，未停止任何服务。

该次副本的证据数字（源指纹 `6e9b574b…`）：

- dry-run 命中 `2500` 条（`USER_MESSAGE` 468 + `CHARACTER_MESSAGE` 2032），`conversation_events` 原有 `2438` 条全部为 Group。
- apply：迁移 `2500`、隔离 `0`，Direct 会话 `38` 个；逐行比对正文/`event_time`/epoch/类型/metadata/actor/owner 不一致数为 `0`；含媒体/贴纸类 metadata 的 `151` 行原样保留；`PRAGMA integrity_check` 为 `ok`，`foreign_key_check` 为空。
- 重复 apply：`skipped=2500`，两表行数与序列值不变。
- 第 3 批后模拟中断再续跑：alias 与 Direct 行与一次跑完逐行相等。
- `sqlite_sequence` 预留到 `1000000000031994`（`FLOOR + 32003 - 1`，取源表最大 id；Direct 消息最大 id 为 31994）。

### 15.7 实施中补上的跨域水位

第 5.2 节的“调度水位”只覆盖了 `latest_direct_event_id`。World 的 BROWSE 重排门槛 `world_activity._browse_signal_event_id` 也把“用户/角色消息”当成信号，但它只查旧 `events`：切写后新对话永远无法重开预算门。两个域的 id 空间互不可比，因此改为**成对水位** `(signal_event_id, signal_fact_id)`，各自与上次记录比较；旧运行记录缺 `signal_fact_id` 时按 `SIGNAL_WATERMARK_INIT` 重排一次。
