# Memory Design

本文只定义已经确认的 Memory 原则和 V0 baseline。

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

不采用“先把所有 Memory 扔进大模型扫描，后面再加向量检索”的过渡方案。

正式链路从 V0 即为：

`Memory text -> Embedding -> SQLite BLOB -> Vector Recall -> Runtime`

SQLite 负责持久化；Vector Retrieval 是可替换索引层。当前用 NumPy 扫描 active memory，未来只有在 benchmark 证明性能不够时才换 ANN/sqlite-vec/FAISS 等实现。

## 4. V0 Recall baseline

当前只是实验基线：

`score = 0.70 * semantic + 0.20 * recency + 0.10 * importance`

这些权重不是产品结论，必须通过 Eval 调整。

Recall 还有两条硬规则：

- 只能 Recall `event_time <= now` 的 Memory，防止虚拟时间中的未来泄漏。
- Embedding 维度变化后，旧向量不混用；使用 `character-memory reembed` 重建。

长期 Recall 方向是：semantic relevance、recency、importance、emotional salience、relationship relevance、associative activation 等信号共同作用，但 V0 不提前实现未验证复杂度。

## 5. 多粒度记忆方向

已经确认长期需要保留多层信息，而不是“不断总结然后删除原文”：

`Raw Event -> Episode -> Daily/Diary -> Long-term Landmark`

高层 Memory 不能替代底层 Event。未来做 consolidation 时，应保留 landmark，并允许追溯到真实经历。

## 6. 尚未决定的部分

以下故意没有写死：

- 什么 Event 一定写入长期 Memory。
- Memory Writer 的最终 Prompt/算法。
- Consolidation 周期与阈值。
- Forgetting / Reconsolidation 具体策略。
- 最终 Recall ranking 公式。

这些必须由 Eval 和真实长期运行数据决定，而不是现在凭感觉复杂化。
