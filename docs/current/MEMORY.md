# Memory Design

本文定义当前已经确认的 Memory 原则和 baseline。Memory 仍然是 Persistent Person 的核心研究面之一，但不会为了“更像长期记忆系统”提前堆复杂层次。

## 1. Durable Facts 是历史事实源

原始经历先进入它所属的 durable fact store，而不是为了形式统一全部复制进一个 `events` 表。

```text
Direct conversation fact -> conversation_events (conversations.type='DIRECT')
Group shared fact        -> conversation_events (conversations.type='GROUP')
Direct non-chat event    -> events
Space shared fact        -> space_*
Media fact               -> media_assets

Durable Facts = Source of Truth
Memory / Mental State / Diary / Summary = derived cognition
```

Direct 与 Group 的可见对话事实（`USER_MESSAGE` / `CHARACTER_MESSAGE`）现在共用一张物理表，域由 `conversations.type` 与 `FactRef.kind`（`EVENT` / `CONVERSATION_EVENT`）区分，不再由表名区分。Direct 写入只有一个入口 `ConversationFactWriter`；旧的 `events` 消息行作为只读归档永久保留，产品代码不再读写它们，历史旧 ID 通过 `conversation_fact_aliases` 解析。迁移边界、ID 区间与回滚见 [CONVERSATION_FACT_MIGRATION.md](CONVERSATION_FACT_MIGRATION.md)。

`TIME_TICK`、`ACTION`、`VISUAL_OBSERVATION`、`PROACTIVE_INTENT`、`LIFE_EVENT`、`DIARY`、`SOCIAL_POST`、`SPACE_*`、`WORLD_OBSERVATION` 仍留在 `events`，各有自己的运行时 owner。

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

正常 PersonReaction（含视觉与可选展示子契约）在同一次模型反应中接收上述六类的简短语义说明，候选用 `content` / `memory_type` / `importance` 表达。按内容主体、真实来源与参与关系分类，而非把 USER_MESSAGE 一律当 USER 或把 TIME_TICK 当已发生生活。SHARED 可以记录实际共同讨论或达成的约定，但计划不代表已执行，讨论不代表现实行动已完成；SELF/LIFE 不能冒认用户、第三方或文章里的经历。类型只是语义注释，不是事实证明或权限。

新 MemoryCandidate 的类型限定为上述六类；合法字符串去除首尾空白并转大写。未知、缺失、空或非字符串类型保守归为 EPISODIC，保留其他有效候选字段，不因此重试或丢弃有效回复。已持久化 Memory 保持既有字符串读取兼容，不回填历史类型。来源 Event ID、事件时间和准入/去重逻辑不变；分类不增加模型调用。模型仍可能误分类，不能用类型合法替代来源核验或真实体验验收。


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

### 来源没有性别依据时不保留第三人称性别

Prompt 已经要求来源忠实（`_PERSON_SOURCE_FIDELITY`），但来源事件完全没有性别信息时，模型仍会把用户写成「他」或「她」，候选随后成为长期 Memory（issue #662）。该规则因此改由确定性代码在 admission 阶段执行（`memory/pronoun_fidelity.py`），位于模型之后、durable write 之前：

- 证据范围：`source_text` 只含用户自己的话——当前事件仅在它是 `USER_MESSAGE` 时计入，加上更早的 `USER_MESSAGE`。`TIME_TICK`、`PROACTIVE_INTENT` 和人物自己的 `CHARACTER_MESSAGE` 都不是来源。人物自身设定（Persona）只在 SELF / LIFE / DIARY 这类关于人物本人的类型里算证据；人物性别不是用户性别，人物上一轮自己的说法也不能给下一次猜测授权。
- 判定：证据里出现任何性别标记（第三人称代词、性别自述、称谓或亲属词）就视为有依据，候选原样保留。
- 中性构词不误判：「其他」「吉他」「他人」「他乡」和「字母」「分母」「师父」「弟子」等不是人称代词或性别证据，既不关闭守卫，也不会被改写成「其TA」；拉丁中性代词 `TA` 必须整词匹配，`data`/`start` 一类普通英文词不算证据。
- 处理：没有依据时把「他/她/祂」就地替换为中性「TA」。不丢弃候选——拒绝会让用户真实陈述的事实静默消失；替换保留指代对象，只去掉没有依据的性别。
- Trace：`gender_pronoun_neutralized` 记录替换次数，`normalized_content` 记录实际写入文本，`candidate` 仍保存模型原始输出。
- 不增加模型调用，不改变 actions 契约。

Direct/Group 的本轮认知执行回执与 Memory / Mental State / Intent、可见事件和
Trace 在同一事务中完成提交。已知 COMMITTED 的来源重放只读取原结果，不再
更新认知或调用模型；事务失败保留原始事实并记录保守执行状态。UNKNOWN /
调用中断不自动重做，不能靠 Memory 近似去重代替整轮执行幂等。回执及恢复
边界见 [Conversation Runtime](CONVERSATION_RUNTIME.md#per-person-cognition-execution-receipts)。

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

Direct/Group 的 Relationship/Recalled Memory，以及 Space/World 的记忆投影使用同一纯渲染函数，保留每条 Memory 的 `event_time` 和 `source_event_id`。时间输出为显式 UTC；旧 naive 时间沿用既有按 UTC 解释的约定。缺失时间或来源标为 `unknown`，不使用本轮时间回填。该时间是 Memory 关联事件的时间锚点，不能推定内容中每件事发生于此时、仍然有效，或把旧内容的“今天/明天”直接解释为当前日期。此投影不修改 Memory、来源事实、召回顺序/数量，也不新增模型调用。

### Bounded candidate set

长期运行不再在每一轮把该 Character 的全部 active Memory BLOB 解包并做 NumPy cosine。SQLite 构造近期、高重要性和显式固定记忆的候选并集：

```text
recent active memories      <= 768
highest-importance memories <= 256
recall-only USER/SHARED supplement <= 256 (highest importance, then newest)
all eligible pinned memories (no count cap)
                     ↓ de-duplicate by id
recall vector working set   <= 1280 + eligible pinned count
admission working set       <= 1024 + eligible pinned count (no supplement)
```

“highest importance”分支保证很老但重要的 Memory 不会仅因时间久就从候选集消失。全部有效固定记忆都会参加排序，重复 ID 只保留一次；大量固定会增加计算开销，固定并不保证全部进入最终 Context（默认最多召回 8 条）。Recall 单独请求一个最多 256 条 USER/SHARED 的候选分支，沿用同人物、active 与 event_time 屏障；默认 store 读取及 admission 不请求此分支。该有界分支让旧关系记忆不必与 Space 动态争同一个候选名额，但不能保证所有历史关系记忆永远在候选集内。

评分公式不变。对于本次候选中稳定排序的语义 top-K（K 为本次返回上限），最多保护其中两条 cosine > 0 的 USER/SHARED，其余位置按原综合 score 补满，最终仍按 score 输出，条数不增加。仅有关系类型但语义名次未达前 K 的条目不受保护；零 query 向量也不触发保护。固定记忆继续保留原加分，不等于必定入选。这里使用相对语义名次，不新增模型相关的固定余弦阈值；仍没有全局语义最低阈值，因此没有相关记忆时也可能返回不相关条目，不能把该策略当成已校准的相关性判断。

Admission 的 exact duplicate 另走 SQLite 全 active 精确文本查询，以与运行时一致的 Unicode `strip().casefold()` 规则比较，因此一条很老的完全相同 Memory 即使不在 vector shortlist 中，也不会重复写入。Semantic near-duplicate 检查只对原 bounded candidate set 做 cosine，不启用 Recall 专用的关系候选补充或位置保护。

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

Memory API 的每个记忆响应还包含独立的 `provenance`：
`ref / status / fact`。`FactRef` 使用 SQLite signed int64 范围内的严格正整数 ID 与 `EVENT` 或
`CONVERSATION_EVENT` 类型；后者必须带 `conversation_id`。当前从原有
`source_event_id` / GROUP metadata 解析，不新增事实副本、不改表或回填历史。
状态为 `UNRECORDED`（无来源）、`INVALID`（引用格式无效）、`UNRESOLVED`
（不存在、损坏或无资格读取）、`RESOLVED`（已读取真实事实）。错误群聊引用
不能回退到同号 Direct Event；Direct 来源必须属于该记忆人物。

Group 来源要求精确会话、已记录的 turn 匹配，以及该人物对同一源事件的
持久 Trace 或其本人发出的真实消息见证；当前在群里不等于当时经历过。
归档或移出不抹掉历史见证。读取不初始化 Group schema、不调用模型或
embedding；未知/损坏来源不推定原文。更正、固定、忘记、恢复仍保留原始来源。
旧 `source` 响应保持兼容，Group 原文仅在新增 `provenance.fact` 中提供，
现有界面不会因此新增来源正文。数据库故障仍报错，不冒充“无来源”。

可重复的真实 HTTP / 重启验收：
`python scripts/validate_memory_provenance.py --output <evidence.json>`。
它使用生产 API 组合及 Memory routes、临时配置/人物/SQLite、deterministic
embedding，与真实回环 HTTP 子进程；验证同号跨域、归属、治理与重开，
记录确切 commit、dirty 状态、事实哈希和零模型调用。它不是模型品质或视觉验收。

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

同一响应中的 `cognition` 是有来源的定向关系认知，不是新的关系状态表。它只从该人物自己的 Memory 及其 durable fact 派生：A 对 B 与 B 对 A 分开计算，绝不互相推断；Memory 只有在直接来源是 B 的真实消息/评论，或群聊 Trace 证明 A 实际看到过 B 的真实消息时，才计入 A 对 B 的认知。同群、同 turn、成员身份或看到动态本身都不构成互动凭据。每条认知保留 Memory ID、记忆时间以及来源 Event/Conversation Event ID、actor、时间和正文节选；群聊输入另标 `WITNESSED_INPUT`。Memory 被更正（`superseded_by`）、忘记（`active=0`）、来源删除/撤回（来源事件缺失或 `hidden`）或来源变为 SIMULATED 时，旧认知标记为 `SUPERSEDED` / `FORGOTTEN` / `INVALIDATED` 且 `asserted=false`，不再作为当前断言；SIMULATED 内容整体排除，只保留计数。更正与忘记复用现有 Memory governance（更正创建新版本、忘记只切换派生状态），不删除原始 Memory/Event，也不允许通过关系视图回滚事实。该投影有界（默认最多扫描最近 400 条 Memory、600 条 Trace、输出 50 条），不新增表、不调用模型、不写库；缺失数据保持未知，不推断好感、关系等级或对方态度。关系视图展示当前断言、已失效历史、依据、变化历史、边界和未完成意图，并可在有来源的当前认知上执行更正或忘记/恢复。

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

## Real-scope recall and admission

Real Person candidate queries exclude explicit simulated/unknown/malformed
`experience_scope` metadata before recent/importance/pinned/type limits; vector
recall also filters before ranking. Exact duplicate checks use the same boundary
so a simulated memory cannot suppress admission of a real one. Raw Memory
Inspector/audit storage remains intact. Scope rules and runtime source/commit
guards belong to [Person Runtime](PERSON_RUNTIME.md#real-experience-boundary).
This is a read/admission guard, not a branch-local memory store or a simulation
reflection-publishing API.

### Space facts and derived events

Space source facts (`space_posts`/`space_comments`) store an optional
`experience_scope`. A row without a stored scope keeps the legacy real-world
meaning; an explicit scope is validated on write as schema v1 `REAL`/`SIMULATED`
(malformed or unknown values are rejected). When `SPACE_POST_SEEN` or
`SPACE_COMMENT_RECEIVED` is synthesized from a stored post or comment, that
fact's scope is copied verbatim into the event metadata; a source without a
scope contributes no scope key, so it stays legacy-real. Replies, reactions,
views and notifications are not source facts and carry no scope of their own;
a simulation producer must stamp every post and comment it writes, including
in-thread replies. A corrupt stored scope reads back as a fail-closed non-real
marker rather than being silently treated as legacy-real.
