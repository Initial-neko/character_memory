# Research References

本文只保留已经确认对当前方向有直接启发的研究。它们是参考，不代表本项目复制其完整架构。

## Generative Agents — UIST 2023

Joon Sung Park et al., *Generative Agents: Interactive Simulacra of Human Behavior*.

- Paper: <https://doi.org/10.1145/3586183.3606763>
- 关键启发：natural-language experience log、dynamic retrieval、reflection/planning，以及人物拥有自己的日常生活。
- 对本项目的影响：保留 Event/Life/Diary/Memory/Time 的完整闭环。
- 不直接照搬：V0 不做完整小镇仿真，也不立即复制复杂 reflection pipeline。

## Persistent Personas? — EACL 2026

Pedro Henrique Luz de Araujo et al., *Persistent Personas? Role-Playing, Instruction Following, and Safety in Extended Interactions*.

- Paper: <https://aclanthology.org/2026.eacl-long.246/>
- 关键结果：超过 100 轮的长对话中 Persona fidelity 会下降，目标导向对话尤其明显。
- 对本项目的影响：Persona Eval 必须覆盖长对话，不允许只评 single-turn impression。

## Memory-Driven Role-Playing / MREval / MRBench — ACL Findings 2026

Kai Wang et al., *Memory-Driven Role-Playing: Evaluation and Enhancement of Persona Knowledge Utilization in LLMs*.

- Paper: <https://aclanthology.org/2026.findings-acl.1175/>
- 关键框架：Anchoring、Selecting、Bounding、Enacting 四阶段 memory-driven role-play evaluation。
- 对本项目的影响：Memory 的价值不是“搜到了事实”就结束，而是 Recall 之后是否正确影响人物行为。

## Mem2ActBench — ACL 2026

Yiting Shen et al., *Mem2ActBench: A Benchmark for Evaluating Long-Term Memory Utilization in Task-Oriented Autonomous Agents*.

- Paper: <https://aclanthology.org/2026.acl-long.370/>
- 关键启发：被动事实问答不足以证明长期 Memory 有效，Memory 应能主动驱动后续 Action。
- 对本项目的影响：Eval 需要检查共享经历是否改变回复、沉默和主动行为，而不只测试 recall QA。

## Human-Inspired Memory Architecture — Microsoft Research, 2026

Doga Kerestecioglu et al., *Human-Inspired Memory Architecture for LLM Agents*.

- Research page: <https://www.microsoft.com/en-us/research/publication/human-inspired-memory-architecture-for-llm-agents/>
- 研究了 consolidation、interference-based forgetting、reconsolidation、hybrid multi-cue retrieval 等机制。
- 对本项目的影响：这些是未来 Memory Policy 的候选方向，但 V0 不提前实现；先保留完整 Event Log 和可重建 Memory，等 Eval 证明需要后再加入。

## 对现有 Memory/Agent 基础设施的结论

之前讨论过 Mem0、Letta、Graphiti/Zep 等系统。结论保持：它们可以提供 memory storage、context runtime、temporal relation 等局部思想，但都不直接等于本项目的 Persistent Person Engine。

当前不把任何一个现有框架作为整体基础；核心实验资产仍然是：

- Persona Schema / Persona Policy
- Memory Policy
- Recall Policy
- Behavioral Policy
- Eval Dataset
