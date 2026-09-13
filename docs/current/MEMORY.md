# Memory Design

本文定义当前已经确认的 Memory 原则和 baseline。Memory 仍然是 Persistent Person 的核心研究面之一，但不会为了“更像长期记忆系统”提前堆复杂层次。

## 1. Event Log 是历史事实源

原始经历首先进入 append-only Event Log。

```text
Event Log = Source of Truth
Memory / Mental State / Diary / Summary = derived cognition
```

因此：

- Memory 写错，不修改原始 Event；
- Memory 策略未来换版本，可以从 Event 重建；
- Diary 不能替代当天真实事件；
- 每条派生 Memory 保留 `source_event_id`；
- 图片/语音等媒体二进制不是 Memory，值得长期记住的意义应语言化后进入正常 Memory Candidate。

群聊尤其要区分：房间里的共享事实只在 `conversation_events` 中保存一次，不为每个人复制一份“原始事实”；每个 Character 可以基于同一事实形成不同 Memory。

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

当前 SentenceTransformer wrapper 优先使用本地 HuggingFace cache；本地没有时才 fallback 到 Hub 获取模型。

因此要区分：

- **下载缓存**：避免每次从网络重新下载模型；
- **进程加载**：每次 Character Runtime 冷启动仍要 import torch/transformers 并把权重从磁盘加载到内存。

冷启动耗时不能和 steady-state Recall latency 混为一谈。

SQLite 当前负责持久化；Vector retrieval 是可替换索引层。只有 benchmark 证明 NumPy full scan 不够时，才讨论 FTS/vector extension/FAISS/专用向量数据库。

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
```

硬规则：

- 只能 Recall `event_time <= now` 的 Memory，避免研究模式未来泄漏；
- embedding 维度变化后，旧向量不混用；使用 re-embed 工具重建；
- Recall 结果只是 Context，不等于人物必须在回复中提起它。

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

## 9. 尚未决定

以下故意没有写死：

- 最终 Memory Writer 算法；
- admission threshold 是否按 Memory Type 动态调整；
- consolidation 周期；
- forgetting / reconsolidation；
- 最终 Recall ranking；
- 是否需要 ANN / sqlite-vec / dedicated vector store；
- relationship-specific memory state 是否应成为独立层。

这些应由 Eval、真实长期运行数据和性能测量决定，而不是先把架构画复杂。
