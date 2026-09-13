# Eval Plan

Persistent Person 不能靠“看起来挺像”来迭代。Prompt、Memory、Recall、Action Policy 的改动都应该进入固定 Eval。

## 1. 第一批核心维度

### Persona Distinctiveness

同一场景交给不同 Persona，盲评是否能识别是谁。目标是高 inter-persona variance、适度 intra-persona variance。

### Action Consistency

给定 Persona、Mental State、Recent Experience、Current Event，最终表达/沉默/追问是否合理。

当前 P0 重点检查：

- 一轮最多 3 个可见 Action。
- `actions=[]` 是合法沉默。
- 明确要求“不用回复”时人物能否真的不回复。
- 多消息/emoji 是否自然，而不是机械拆句。

### Recall Precision

该想起的内容是否进入 Top-K；不相关 Memory 是否侵入上下文；是否发生 false recall。

### Memory Admission Precision

不再只检查“模型有没有生成 candidate”，还检查候选最后是否：

- `WRITE`
- `SKIP_LOW_VALUE`
- `SKIP_DUPLICATE`

普通寒暄不应该污染长期 Memory；明确的重要未来事件应该能进入 Memory。

### Memory Provenance

每个派生 Memory 是否可以回溯到原始 Event。

### Relationship / Re-encounter

共同经历和真实时间间隔是否影响后续行为，而不是只会复述用户事实。

当前 Context 明确提供：

- 上次聊天时间。
- 距离上次聊天多久。

Eval 要检查 Day N 再次见面时旧事是否能自然回来，同时禁止机械的“好久不见”。

### Safe Thought Summary

`perception / reaction` 用于 Developer Inspector 和用户可选“想法”视图。

它们必须：

- 简短。
- 对调试 Persona 有价值。
- 不包含 raw hidden chain-of-thought。

### Long-term Persona Continuity

在 10 / 30 / 100+ turns 检查：

- 是否仍像同一个人。
- 重要 landmark 是否还存在。
- 共同经历是否合理影响后续行为。
- 是否出现客服化、无条件迎合或 Persona Drift。

## 2. P0 Relationship Suite

`evals/p0_relationship.jsonl` 当前有 **24 条**：

`4 Characters × 6 scenarios`

每个人都经历相同的：

1. 普通聊天 / Memory Precision。
2. 负面情绪 / 自然追问 / Safe Summary。
3. 观点冲突 / 是否保持自己的判断。
4. 明确要求“不用回复” / Silence Boundary。
5. 重要未来事件 / Memory Write。
6. 7 天后重逢 / Recall + Relationship Time。

CLI：

```bash
uv run character-memory eval evals/p0_relationship.jsonl
```

Eval 使用临时 SQLite，不污染正式人物数据。

输出除了逐 case pass/fail，还按 tag 汇总，例如：

- `silence`
- `memory_precision`
- `memory_recall`
- `reencounter`
- `persona`
- `disagreement`
- `safe_summary`

注意：这些规则只能验证结构化、可观测合同。**Persona 是否真的“像 Momo/Haru/Rei/Rin”仍需要盲评或 Judge Model，不能靠关键词断言。**

## 3. Smoke Eval

`evals/smoke.jsonl` 保留为最小 provider/runtime 冒烟测试。

`EvalRunner` 当前支持：

- 单 Runtime 或多 Character Runtime map。
- allowed actions。
- silence。
- message count。
- must/must-not contain。
- safe summary presence。
- min/max Memory write。
- minimum Recall count。
- Context contains。

## 4. 后续但不是本轮 P0

后续再加入：

- Model-as-Judge adapter。
- Persona Identification Accuracy。
- 10/50/100/500 turn persona decay suite。
- 30/100/365-day continuity suite。
- Forced Recall Rate。
- Customer-service tone score。
- Multi-action naturalness score。

Judge Model 必须和 Person Runtime 解耦，避免“换 Judge 等于改产品行为”。
