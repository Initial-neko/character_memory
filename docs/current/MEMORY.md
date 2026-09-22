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
- Recall 结果只是 Context，不等于人物必须在回复中提起它。

### Bounded candidate set

长期运行不再在每一轮把该 Character 的全部 active Memory BLOB 解包并做 NumPy cosine。SQLite 先构造有上限的候选并集：

```text
recent active memories      <= 768
highest-importance memories <= 256
                     ↓ de-duplicate by id
vector ranking working set  <= 1024
```

“highest importance”分支保证很老但重要的 Memory 不会仅因时间久就从候选集消失。最终候选仍使用原来的 semantic + recency + importance 公式排序。

Admission 的 exact duplicate 另走 SQLite 全 active 精确文本查询，因此一条很老的完全相同 Memory 即使不在 vector shortlist 中，也不会重复写入。Semantic near-duplicate 检查只对 bounded candidate set 做 cosine。

这不是 ANN，也不是最终长期记忆算法；它只是用低复杂度改动把常规每轮 Python/NumPy 工作量从随 N 无界增长收成固定上限。是否引入 sqlite-vec/FAISS/专用向量库仍由长期 benchmark 决定。

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

- 查看 active / inactive Memory、importance、来源和原始 Direct Event 摘要；
- **固定 / 取消固定**：`pinned` Memory 永远进入 bounded recall candidate union，并获得小幅 ranking bonus；
- **忘记 / 恢复**：只切换派生 Memory 的 `active`，不删除原始 durable fact；
- **纠正**：创建一条新 Memory，旧 Memory 变为 inactive，并通过 `superseded_by` 指向新版本；不 UPDATE 原文本覆盖历史。

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

World appraisal 的 `summary` 只服务本轮理解/表达，本身永远不是长期 Memory。只有 appraisal 明确产生非空 `personal_memory`，并且内容描述的是“这次观察对人物本人形成的持续兴趣、经历或反思”，才允许送入同一个 PersonRuntime Memory admission。

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
