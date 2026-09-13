# P0.5 — Chat Rhythm & Persona Creation

本阶段只改善 Phase 1 的直接聊天体验，不扩展 Life / Tool / Voice / Feed。

## 1. Chat Delivery Rhythm

Runtime 仍一次性产生并持久化 `actions[0..3]`，不改变 Event / Trace / Memory 语义。

WebUI 在收到结果后按 Action 顺序逐条呈现：

- 第一条沿用真实 Provider 等待时间；
- 后续 `MESSAGE` 使用约 260~1200ms 的轻量间隔；
- `EMOJI` 使用更短间隔；
- 两条之间短暂重新显示 typing indicator；
- 不按真实打字速度长时间阻塞用户。

因此节奏属于 **delivery presentation**，不是 Runtime decision。

## 2. New Character Flow

左侧人物列表提供 `＋ 新建人物`。

流程：

```text
自然语言描述
  ↓
AI Persona Draft
  ↓
用户可读人物说明
  ↓
可选高级编辑
  ↓
明确创建
  ↓
personas/<character_id>/persona.yaml
  ↓
动态注册 Runtime（若 Runtime 已加载）
  ↓
立即进入聊天
```

AI 输出永远只是 Draft。未点击“创建人物”前，不写 Persona 文件、不创建正式 Runtime、不产生聊天 Event/Memory。

## 3. Persona Builder Contract

Draft 包含：

- name / age
- identity / tagline
- description（普通用户阅读）
- personality
- conversation
- expression
- questions
- silence
- initiative
- disagreement
- care
- boundaries

保存时继续转换成当前 Persona YAML 结构，因此 `personas/*/persona.yaml` 仍是单一人物定义源，不增加第二张 Character Registry 表。

## 4. Product Constraints

Persona Builder 的目标是创建一个长期连贯、有自主判断和边界的人，而不是 engagement 角色生成器。

必须保持：

- 不预设人物必须喜欢用户；
- 不无条件迎合；
- 不用依赖或快速亲密作为留存机制；
- 不支持色情、擦边或性化人物设定；
- 关系仍通过真实 Event / Shared Experience 演进。

## 5. Why No Draft Chat Preview Yet

本轮故意不做“创建前试聊”。

若直接复用正式 Runtime，会在确认创建前污染 Event / Memory；若引入临时 Runtime，则需要额外的临时会话与清理生命周期。当前核心闭环不依赖该功能，因此等真实 UX 验证证明必要后再设计。

## 6. Memory Testing Characters

为了更容易暴露长期记忆问题，创建页给出一些鲜明但非强制的人设方向，例如：

- 平时克制、真正感兴趣时突然很投入的反差角色；
- 会收集奇怪小事、容易形成 Shared Memory 的脑洞角色；
- 温柔但会认真反驳的稳定判断型角色；
- 喜欢观察细节、会形成独特关联的神秘观察者。

测试重点不是角色是否“夸张”，而是：

- 普通寒暄是否被错误记住；
- 鲜明共同经历是否形成 Memory；
- 数天后能否自然 Recall；
- Recall 是否影响人物自己的表达方式，而不是机械复述事实。
