# Eval Plan

Persistent Person 不能靠“看起来挺像”来迭代。Prompt、Memory、Recall、Action Policy 的改动都应该进入固定 Eval。

## 1. 第一批核心维度

### Persona Distinctiveness

同一场景交给不同 Persona，盲评是否能识别是谁。目标是高 inter-persona variance、适度 intra-persona variance。

### Action Consistency

给定 Persona、Mental State、Recent Experience、Current Event，最终 Reply/Silence/Defer/Proactive 是否合理。

### Recall Precision

该想起的内容是否进入 Top-K；不相关 Memory 是否侵入上下文；是否发生 false recall。

### Memory Provenance

每个派生 Memory 是否可以回溯到原始 Event。

### Relationship Behavior

共同经历是否真实影响后续行为，而不是只会复述用户事实。

### Life Continuity

在 Day 1 / 7 / 30 / 100 等时间点检查：

- 是否仍像同一个人。
- 重要 landmark 是否还存在。
- 生活经历是否合理影响后续行为。
- 是否出现自相矛盾。
- Diary 是否逐步漂移或编造历史。

### Proactive Precision

主动联系是否有真实、可追溯理由，而不是为了提高互动率制造泛化消息。

## 2. Rin 30 Days Test

V0 的第一个完整 Milestone：

1. 创建一个固定 Persona。
2. 与用户进行少量真实对话。
3. 推进虚拟时间 30 天。
4. 允许 Life Event、Diary、Memory、Intent、主动/沉默行为自然累积。
5. 用 Inspector 回放整个 Timeline。

最终必须能回答：

- 她是谁？
- 30 天发生了什么？
- 她记得什么，为什么？
- 哪些 Memory 被 Recall，哪些没有？
- 为什么某次没有回复？
- 为什么某天主动联系？
- Day 30 是否仍然是 Day 1 的那个人？

## 3. 当前代码中的 Eval

`evals/*.jsonl` + `EvalRunner` 目前只是 regression harness：allowed action、消息必须/禁止包含文本等。

CLI 会为 Eval 创建临时 SQLite，避免测试历史相互污染真实人物数据库。

后续再加入：

- Model-as-Judge adapter。
- Persona Identification Accuracy。
- Memory should-recall / should-suppress 标注集。
- 10/50/100/500 turn persona decay suite。
- 30/100/365-day continuity suite。

Judge Model 必须和 Person Runtime 解耦，避免“换 Judge 等于改产品行为”。
