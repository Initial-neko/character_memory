# Memory Design

本文定义当前已经确认的 Memory 原则和 baseline。Memory 仍然是 Persistent Person 的核心研究面之一，但不会为了“更像长期记忆系统”提前堆复杂层次。

## 1. Durable Facts 是历史事实源

原始经历先进入它所属的 durable fact store，而不是为了形式统一全部复制进一个 `events` 表。

```text
Direct fact        -> events
Group shared fact  -> conversation_events
Space shared fact  -> space_*
Media fact         -> media_assets

Durable Facts = Source of Truth
Memory / Mental State / Diary / Summary = derived cognition
```

因此：

- Memory 写错，不修改原始 durable fact；
- Memory 策略未来换版本，可以从有 provenance 的事实重新推导；
- Diary 不能替代当天真实经历；
- 派生 Memory 应保留可追溯 source/provenance；
- 图片/语音等媒体二进制不是 Memory，值得长期记住的意义应语言化后进入正常 Memory Candidate。

群聊里的共享事实只在 `conversation_events` 保存一次；Space post/comment/like/view 也有自己的 shared fact 表。每个 Character 可以基于同一共享事实形成不同的认知派生。

## 2. Memory 使用自然语言

当前选择 **language-level memory**，不先做复杂 Knowledge Graph / world-state ontology。

常见类型：

- `USER`：关于用户、未来值得想起的信息；
- `SELF`：人物关于自己的经历、判断或变化；
- `SHARED`：人物与用户共同经历；
- `LIFE`：人物自己的生活事件；
- `DIARY`：一天结束后的主观记录；
- `EPISODIC`：尚未进一步分类的事件性记忆。

重要原则：人物不只“记用户”，也要能记得自己的经历和共同历史。

## 3. Embedding path

正式路径：

```text
Memory text
  -> Embedding
  -> SQLite float32 BLOB
  -> Vector Recall
  -> Runtime context
```

默认：

```text
embedding_provider: sentence-transformers
embedding_model: BAAI/bge-small-zh-v1.5
```

SentenceTransformer runtime 是 **strict-offline**：

```text
SentenceTransformer(model, local_files_only=True)
```

正常 Character Runtime 启动和第一句话都不会 fallback 到 Hugging Face Hub。模型获取只发生在明确的 setup/prefetch 阶段：

```bash
bash scripts/setup-media-models.sh
# 或
uv run python scripts/prefetch_embedding_model.py
```

Web Runtime 启动后会立即在后台 warm `AppBundle` / Embedding；`/health` 仍然先可用，并通过 `runtime_loading / runtime_loaded / runtime_error` 暴露状态。这样冷启动的 torch/transformers import 与权重加载不会隐藏到“用户第一句话”里。

仍要区分：

- **prefetch/cache**：允许联网取得模型；
- **process warmup**：只从本地 cache 加载；
- **steady-state recall**：聊天中的向量计算。

SQLite 继续负责持久化；当前不引入专用向量数据库。

## 4. Memory Candidate 不是 Memory Write

Person Model 在正常 reaction 中可以返回：

```text
memory_candidates[]
```

不增加第二次“要不要记住”的 LLM 调用。

Candidate 再进入 deterministic admission：

```text
Memory Candidate
      ↓
importance < 0.35 ? ---- yes -> SKIP_LOW_VALUE
      ↓ no
exact duplicate ? -------- yes -> SKIP_DUPLICATE
      ↓ no
cosine >= 0.93 ? ---------- yes -> SKIP_DUPLICATE
      ↓ no
WRITE
```

Embedding 与第一轮 exact/near-duplicate 判断在事务外执行，避免远端或本地模型计算占住 SQLite 写锁。真正写入前会在 derived-state 事务内用当前 active Memory 再检查一次；Direct、Group、Space/World 等渠道即使并发形成相同或近似候选，也只有一个可以写入。输掉竞态的候选在 Trace 中记录为 `SKIP_DUPLICATE_AT_COMMIT`，并保留命中的 Memory ID 与 similarity。

事务内的提交重查以当前 active Memory 集合为准，不受来源 `event_time` 上限限制：较新来源先提交、较旧来源迟到提交时，迟到候选也能看到已提交的新条目并记为 `SKIP_DUPLICATE_AT_COMMIT`。Context / Recall 仍保留各自的 `event_time <= at` 未来屏障；提交去重不复用该读取边界。

这些是 Eval baseline，不是永久产品定律。

Trace 记录：

```text
candidate
decision
similarity
duplicate_memory_id
```

## 5. Optional metadata resilience

Memory Candidate 是 outward reaction 的辅助派生信息。

因此类似：

```json
{"content":"用户提到了一个偏好","importance":4}
```

不会因为 importance 评分范围漂移就让一个已经合法的 MESSAGE 一起失败。当前 schema 会对 harmless numeric drift 做 bounded normalization；真正缺少 `content` 等无法解释的 candidate 可以单独丢弃。

这个容错不意味着最终 Memory 可以无限制写脏数据：Admission 和最终 `Memory` model 仍保留自己的约束。

优先级是：

```text
有效 outward reaction
  > optional candidate annotation
```

## 6. Recall baseline

当前基线：

```text
score = 0.70 * semantic
      + 0.20 * recency
      + 0.10 * importance
      + 0.12 * pinned
```

硬规则：

- 只能 Recall `event_time <= now` 的 Memory，避免研究模式未来泄漏；
- embedding 维度变化后，旧向量不混用；使用 re-embed 工具重建；
- re-embed 先完成批量向量计算并校验数量，再在一个事务中写入；写入失败会回滚整批，不留下部分重建结果；
- Recall 结果只是 Context，不等于人物必须在回复中提起它。

### Bounded candidate set

长期运行不再在每一轮把该 Character 的全部 active Memory BLOB 解包并做 NumPy cosine。SQLite 构造近期、高重要性和显式固定记忆的候选并集：

```text
recent active memories      <= 768
highest-importance memories <= 256
all eligible pinned memories (no count cap)
                     ↓ de-duplicate by id
vector ranking working set  <= 1024 + eligible pinned count
```

“highest importance”分支保证很老但重要的 Memory 不会仅因时间久就从候选集消失。全部有效固定记忆都会参加排序，重复 ID 只保留一次；大量固定会增加计算开销，固定并不保证全部进入最终 Context（默认最多召回 8 条）。最终候选仍使用 semantic + recency + importance + pinned 公式排序。没有语义最低阈值，相似度为零的记忆仍可能因新近性或重要性入选。

Admission 的 exact duplicate 另走 SQLite 全 active 精确文本查询，以与运行时一致的 Unicode `strip().casefold()` 规则比较，因此一条很老的完全相同 Memory 即使不在 vector shortlist 中，也不会重复写入。Semantic near-duplicate 检查只对 bounded candidate set 做 cosine。

这不是 ANN，也不是最终长期记忆算法；它只是用低复杂度改动限制非固定记忆的每轮 Python/NumPy 工作量；显式固定记忆数量仍会增加工作量。是否引入 sqlite-vec/FAISS/专用向量库仍由长期 benchmark 决定。

长期可研究的信号包括 emotional salience、relationship relevance、associative activation 等，但当前不提前复杂化 ranking。

## 7. 多粒度记忆方向

长期可能需要：

```text
Raw Event
  -> Episode
  -> Daily / Diary
  -> Long-term Landmark
```

但高层摘要不能替代底层 Event。未来 consolidation 必须保留 provenance，并允许追溯到真实经历。

## 8. Group memory semantics

群聊当前模型：

```text
one shared conversation event
   ├─ Character A perceives -> maybe Memory A
   ├─ Character B perceives -> maybe Memory B
   └─ Character C perceives -> maybe Memory C
```

共享事实不能因为三个角色都参与而在事实层复制三次；认知派生可以不同。

某一个 group member 本轮 structured output 失败，也不应该让其他成员失去形成 reaction/memory 的机会。

## 9. Minimal Memory governance

V1 已提供最小的人为干预闭环，入口在 Character Runtime 抽屉的 **Memory Inspector**：

人物生活中的关联记忆可通过 `/?memory_character=<id>&memory_id=<id>`
直达同一 Inspector 卡片。只读 `GET /v1/characters/{character_id}/memories/{memory_id}`
读取指定记忆及来源，不受最近记忆列表的数量限制，不加载人物模型；
人物不存在、记忆不存在或不属于该人物均返回 `404`。

- 查看 active / inactive Memory、importance、来源和原始 Direct Event 摘要；
- **固定 / 取消固定**：`pinned` Memory 永远进入 bounded recall candidate union，并获得小幅 ranking bonus；
- **忘记 / 恢复**：只切换派生 Memory 的 `active`，不删除原始 durable fact；
- **纠正**：创建一条新 Memory，旧 Memory 变为 inactive，并通过 `superseded_by` 指向新版本；不 UPDATE 原文本覆盖历史。

已被纠正的旧版本只作为历史记录保留，不能恢复、固定或再次纠正。
这些约束由存储层在同一锁/事务内检查，而不只依赖 Inspector 隐藏按钮；
对应 HTTP 操作返回 `409`，重复纠正不会创建分叉版本。
需要继续纠正时，应操作当前的新版本。普通忘记的记忆仍可恢复。

因此：

```text
Durable Fact
   ↓
Memory v1  ──纠正──> Memory v2
   │                  ↑
   └ inactive + superseded_by
```

Memory governance API 位于 `memory_web.py`。当前不做复杂的角色记忆分类器、批量规则、知识图谱或来源级策略管理。

### World Observation memory policy

当前正式规则是：

> **看到网页 ≠ 长期记住网页。**

World appraisal 的 `summary` 是本次观察的工作摘要，本身不是长期 Memory。Space World Explore 可以明确产生非空 `personal_memory`；只有描述人物自身持续兴趣、经历或反思的候选，才送入现有 PersonRuntime Memory admission。Personal Browse 与个人 RSS 阅读的保留结果直接形成有来源的 `WORLD_OBSERVATION`，不自动更新 Mental State、创建长期 Memory 或对外表达。观察记录本身仍是合法经历，后续可被检索。

例如：

```text
网页事实：某模型今天价格是多少
-> 不进入长期 Memory，需要时重新查询

人物经历：我发现自己会持续关注这种长期记忆设计
-> 可以成为 Person Memory
```

raw webpage text 仍然是 untrusted data，不直接进入 Memory 或最终 Space prompt。

来源 URL / query 继续保存在 WORLD_OBSERVATION Event metadata 作为 provenance。更复杂的 freshness / confidence / 世界知识缓存暂不实现。

## 10. 尚未决定

以下故意没有写死：

- 最终 Memory Writer 算法；
- admission threshold 是否按 Memory Type 动态调整；
- consolidation 周期；
- forgetting / reconsolidation；
- 最终 Recall ranking；
- 是否需要 ANN / sqlite-vec / dedicated vector store；
- relationship-specific memory state 是否应成为独立层。

这些应由 Eval、真实长期运行数据和性能测量决定，而不是先把架构画复杂。

## Historical observed experiences

PersonContextBuilder includes a separate bounded `observed_events` projection of
actual WORLD_OBSERVATION events. It is not a Memory import and does not query RSS
inventory. Local lexical matching happens across the character's past events
before a result limit is applied, so unrelated newer activity does not itself
exclude a matching old observation. At most four observations enter context,
with a 4000-character display budget, source event IDs, observation timestamps
and source references. Full source metadata remains on the event; oversized
source display values may be truncated. This is keyword recall, not a promise
of arbitrary semantic paraphrase retrieval, and adds no LLM or embedding call.

PERSONAL reads the character's own observations. PUBLIC retains explicit World
Pulse observations and the verified published projection of a Space World
observation. Publication records the actual post ID/content atomically with the
post. Recall checks owner, exact public content and publication time, searches
only published text and removes private appraisal metadata. Personal Browse,
personal RSS and legacy WORLD observations without a publication receipt remain
excluded from the public observation field. This neither expands publication
permissions nor publishes private observations automatically.
This does not claim to implement comprehensive privacy classification of all
legacy Memory. Existing Memory admission and its future-time barrier remain.
Caller-provided recent shared facts retain their actor identities but are
filtered at the requested event time; explicit event exclusion is also applied.

Invalid or non-object legacy Event metadata is exposed as `metadata_status=INVALID`
when decoding Events. IDs, content and encounter time remain intact; missing
sources are not invented, the stored raw metadata is not rewritten, and such
observations cannot qualify for the PUBLIC historical projection. This fallback
only covers Event JSON metadata decoding, not arbitrary database corruption or
all legacy SQL expressions. It does not add model calls or Memory admission.


Chinese observation questions strip common recall framing and terminal question
particles before bounded lexical matching, so a known source title can match
without spaces (for example asking whether a character has read that title).
This is not general Chinese segmentation or semantic paraphrase retrieval.
Only actual personal observation events are queried, with existing character,
time and public-projection filters and the four-result cap.

Rendered experiences include a bounded stored RSS title and Feed-text scope,
plus separately labeled source summary and personal observation. The existing
4,000-character total budget still applies; full provenance remains outside the
prompt. Optional malformed metadata does not manufacture a title or summary.

## Character life observer

`/life` is a user-facing, read-only view of real character records, separate from Dev Console. It reads retained historical LIFE_EVENT/DIARY records but does not offer virtual-life simulation or diary generation. The page shares the user visual shell and uses actual character profiles for its selection list and avatar; timeline/detail views preserve the existing source and fact boundaries. `GET /v1/characters/{character_id}/life` projects Core events, actually participated Group events, own Space posts/comments and actual Space opportunity receipts, durable World decisions and future expression Intents. It does not initialize a character runtime, call a model, fetch external content, write schemas or create observations, Memory or Intent.

The page offers a date (with client UTC offset), literal content/title search, channel filters, life/change/relationship/intent views and bounded keyset paging (default 30, maximum 50). Source identities are namespaced. Unknown characters are rejected, optional absent subsystem tables remain absent, invalid cursors/date/filter inputs are rejected. Records without usable persisted time cannot be placed in a dated timeline.

State comparisons come from persisted Mental State versions or an existing Group trace's before/after values. Memory associations require matching character and the correct Core or Group provenance domain. A source-event association establishes provenance, not a general psychological cause. Already-saved model reaction summaries may be shown as recorded output; raw model responses, prompts, embeddings and hidden reasoning are not returned. A missing reason remains missing.

A real decision opportunity may show no action, failure or refusal; an empty day is not labelled intentional silence. World reading scope is preserved (Feed text is not a fetched original page). The Intent view distinguishes plans and durable resolution/deferral audits from performed activity; current Intents remain future expression, not a general Goal Engine.

Relationship records are actual received Direct messages, Group events processed by the selected character, or actual Space interactions with identifiable counterpart provenance. Peer filtering occurs before paging. An explicit Space reply uses the actual parent commenter as counterpart; an unavailable parent is not replaced with a guessed identity. Own Group messages do not create self relationships. Legacy Group Memory provenance is excluded from Core event matches even when numeric IDs collide, and malformed Memory metadata cannot establish a Group association. Membership alone is not proof of participation; no affection score or inferred relationship state is generated. The page uses the project's existing local Core API access boundary, rather than publishing a new public social feed.

选择人物和互动对象后，关系视图增加只读“近期回顾”：`GET /v1/characters/{character_id}/relationship-review?peer=...&day=...&offset=...` 显示所选日及之前共 7 天内、且不晚于读取时刻的最多 10 条真实互动。正文最多 500 字符，保留 namespaced 来源、渠道和时间，复用来源核验与原会话草稿入口。它与下方当天时间线的渠道/搜索筛选分开。群记录要求所选人物当前加入时间边界，以及同群、时间范围内的实际处理 Trace：可使用 Trace 主 source ID，或实际 `recent_group_event_ids`（最多 14 个整数），或 `shared_history_retrieval.events` 中同群的整数来源 ID（最多 4 条）。正常群轮次的主 source 通常是 User 根事件，不能据此否认后续人物实际读过前一位的回复；也不能仅凭同 turn 推断已读。缺失或畸形输入回执的旧 Trace 仍按已知直接来源保守展示，不回填已读凭据。私聊只归所选人物与 User；其它对象的互动只从有实际对象凭据的群消息或 Space 评论中投影，不展示另一人物的私聊或隐藏状态。

回顾另列当前有效 `PENDING / PROACTIVE_MESSAGE` 意图最多 5 条，查询所有创建日期，不受回顾日期限制。当前可执行对象仅为 User Direct；校验已有源事件属于该人物且为 Direct 用户消息或 Wake，无来源的旧意图明确标记未知并沿用现有 User Direct 兼容行为。过期、已执行、错误对象或群/Space/World 来源不投影成待执行计划；群聊原文邀请不会被解析为已接受或已完成活动。空数据说明未知，不生成日总结、关系评分、Memory、意图或新关系表，也不调用模型。

“围绕这件事聊天”通过只读 `GET /v1/characters/{character_id}/life/conversation?key=...` 核验来源，跳转原有 Direct/Group 会话并准备草稿。URL 只携人物和 namespaced source key；正文从服务端记录读取。查看、跳转、取消均不写事实、不调用模型。用户确认发送后，异步消息入口携 `life_reference={character_id,key}` 再次核验，追加有来源的记录引用并保存同一用户消息的 `metadata.life_reference`，再复用现有 scheduler/SSE。引用声明用户未必亲历，也不是共同活动完成凭据；World 记录正文不等同网页全文。同步旧发送入口拒绝该字段。

Direct/Group 继续聊天入口按 `archived_at` 键存在判定来源人物归档，空值或损坏标记也会在写入用户消息前拒绝；历史记录仍可按原只读边界查看。

首版支持明确的 Core 交流/观察、所选人物实际参与的公开 Group 消息、自己的 Space post/comment。Group 必须仍存在、未归档，用户与所选人物仍为成员，所选人物加入时间不晚于来源。参与凭据仅限本人发言，或同群/同人物且不早于来源、不晚于当前时间的实际 trace：旧 root source ID 精确匹配、`recent_group_event_ids` 前14个整数ID，或 `shared_history_retrieval.events` 前4个对象的同群整数 source ID。畸形 JSON、错群、字符串ID与缺失回执不扩大可见性；不把成员身份单独视为过去参与凭据。Direct 来源须属于所选人物并有已有私聊可续接。人物归档、来源删除/不存在、归属不符、无效时间、无正文、计划/机会类来源明确失败；不建立新会话、不总结整天，不引入自动 Memory admission。

Reading details distinguish the recorded Feed scope, read/available character counts and truncation from the model appraisal. `RSS_FEED_TEXT` titles explicitly name Feed text; linked original pages are not claimed as read. Missing scope counts remain unknown. A malformed Intent execution/expiry time reports `time_status=INVALID`, not a valid not-yet-due verdict. Timeline responses include `undated_count` for otherwise matching records whose source timestamp is missing/invalid; they cannot be assigned to the selected date and are not silently treated as a quiet day.

### External execution details

An existing World activity/observation can link its `capability_request_id` to
the read-only execution list/detail APIs under
`/v1/characters/{character_id}/executions`. The Life page's “查看外部执行” shows
the public task, visible assistant reply, actual source receipts, connected
tools and usage. It adds no simulated life event and does not resume or rerun
the task. Records are scoped to the requested character; another character's
request returns 404. Missing/expired detail files do not break the timeline.

Execution pagination orders absolute execution start time at microsecond
precision, with request ID as the stable tie-breaker; different timezone spellings
do not change chronology. Original timestamps remain visible. Per-day usage uses
the durable local quota day, and incomplete cost/token totals remain unknown.

This is a task/response inspection view, not a complete intermediate tool-call
transcript or a new Memory browser. Tool arguments and step-by-step tool replies
are not currently displayed. Assistant prose is not original webpage text or
proof that a character adopted it. Memory/state links refer only to persisted
cognition effects. Executor configuration and retention ownership belong to
[Architecture](ARCHITECTURE.md), not to Memory admission.
