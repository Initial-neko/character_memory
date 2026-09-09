# Memory Design

本文只定义已经确认的 Memory 原则和当前 baseline。

## 1. Event Log 才是历史事实源

原始经历首先进入 append-only Event Log。

`Event Log` 是 Source of Truth；`Memory`、`Diary`、未来的 Summary 都是从经历派生出来的认知层。

因此：

- Memory 写错，不修改原始 Event。
- Memory Writer 未来换版本，可以从 Event Log 重建。
- Diary 不能替代当天真实事件。
- 每条派生 Memory 必须保留 `source_event_id`。

这是为了避免一次错误总结永久污染人物历史。

## 2. Memory 使用自然语言

当前方向明确选择 **language-level memory**，不先做复杂 World State / Knowledge Graph。

Memory 不只是“用户事实”。至少包括：

- `USER`：关于用户、且未来值得想起的信息。
- `SELF`：人物关于自己的经历、判断或变化。
- `SHARED`：人物与用户共同经历。
- `LIFE`：人物自己的生活事件。
- `DIARY`：一天结束后的主观记录。
- `EPISODIC`：尚未进一步分类的事件性记忆。

真正重要的一点是人物也要“记得自己”。

## 3. 从第一天就 Embedding

正式链路从 V0 即为：

`Memory text -> Embedding -> SQLite BLOB -> Vector Recall -> Runtime`

SQLite 负责持久化；Vector Retrieval 是可替换索引层。当前用 NumPy 扫描 active memory，未来只有在 benchmark 证明性能不够时才换 ANN/sqlite-vec/FAISS 等实现。

## 4. P0 Memory Admission

模型给出 `memory_candidates` 不等于数据库必须全部接受。

当前增加一层非常小的 deterministic admission gate：

```text
Memory Candidate
      ↓
低价值？ ──是──> SKIP_LOW_VALUE
      ↓否
与已有 Memory 完全相同 / 近重复？ ──是──> SKIP_DUPLICATE
      ↓否
     WRITE
```

当前 baseline：

- `importance < 0.35`：`SKIP_LOW_VALUE`
- 文本规范化后完全相同：`SKIP_DUPLICATE`
- Embedding cosine `>= 0.93`：`SKIP_DUPLICATE`
- 其余：`WRITE`

这些阈值是 **Eval baseline，不是最终产品结论**。

Admission 不使用第二次 LLM 调用，避免普通聊天为了“判断要不要记”增加额外模型延迟。

每个候选的结果会写入 Runtime Trace：

```text
candidate
 decision = WRITE / SKIP_LOW_VALUE / SKIP_DUPLICATE
 duplicate_memory_id
 similarity
```

这样 Memory Precision 可以被实际调试，而不是只能看到最后数据库里剩了什么。

未来如果 Eval 证明单纯 importance + duplicate gate 不够，再讨论 Memory Writer/Consolidation，不提前增加复杂架构。

## 5. Recall baseline

当前实验基线：

`score = 0.70 * semantic + 0.20 * recency + 0.10 * importance`

Recall 有两条硬规则：

- 只能 Recall `event_time <= now` 的 Memory，防止虚拟时间中的未来泄漏。
- Embedding 维度变化后，旧向量不混用；使用 `character-memory reembed` 重建。

长期 Recall 方向是 semantic relevance、recency、importance、emotional salience、relationship relevance、associative activation 等信号共同作用，但当前不提前实现未验证复杂度。

## 6. 多粒度记忆方向

长期需要保留多层信息，而不是“不断总结然后删除原文”：

`Raw Event -> Episode -> Daily/Diary -> Long-term Landmark`

高层 Memory 不能替代底层 Event。未来做 consolidation 时，应保留 landmark，并允许追溯到真实经历。

## 7. 尚未决定的部分

以下故意没有写死：

- Memory Writer 的最终 Prompt/算法。
- `0.35 / 0.93` 是否应该调整或按 Memory Type 区分。
- Consolidation 周期与阈值。
- Forgetting / Reconsolidation 具体策略。
- 最终 Recall ranking 公式。

这些必须由 Eval 和真实长期运行数据决定，而不是现在凭感觉复杂化。
