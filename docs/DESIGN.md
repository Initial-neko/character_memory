# Product Design — Persistent AI Person

本文只记录已经确认的产品原则，不描述具体数据库或代码实现。

## 1. 我们在做什么

核心不是“一个更会回答问题的 chatbot”，也不是“先选一个 AI 伴侣然后无限聊天”。

目标是构建 **Persistent AI Person Engine**：

- 同一个人物跨天、跨月仍然可辨认。
- 人物有自己的过去、注意力、观点、边界和未完成事情。
- 关系来自反复相遇和共同经历，而不是用户创建时填写一个关系等级。
- 用户未来回来，是因为想再次找到“这个人”，而不是单纯想“使用 AI”。

长期判断标准：30 天之后，人物是否仍然是同一个人，同时又真实地受过去 30 天经历影响。

## 2. 产品价值边界

已经确定：

- 现在和未来都不支持色情、擦边或性化内容。
- 产品方向应帮助用户向上、向前，而不是通过依赖、刺激或无止境互动制造留存。
- AI 可以比真人更有耐心、更善于记忆和倾听，但不能因此变成永远迎合用户的服务角色。
- 人物可以不同意、沉默、改变话题、保持距离，也可以有自己的生活。
- 推荐和主动行为最终应优化 **Meaningful Relationship Formation**，而不是 CTR、Watch Time 或单纯消息数量。

## 3. 关系如何形成

长期产品循环：

`Discover -> Encounter -> Talk -> Remember -> Re-encounter -> Relationship`

一旦一个临时生成的人物与用户发生了真正有意义的经历，它就应从候选角色变成 Persistent Identity。弱关系可以淡出或休眠，但不等同于删除；未来重新遇见时，过去仍然存在。

重要的长期信号之一是 **User Initiated Character Recall**：用户是否会主动回来寻找某个具体人物。

## 4. Persona 的定位

Persona 必须改变行为选择，而不只是语气。

它至少应该影响：

- 关注什么。
- 如何理解同一个事件。
- 什么值得记住。
- 是否追问、沉默、主动、保持边界。
- 面对冲突和关系变化时如何行动。

目标不是复制现实人物，而是建立一个可评测的 Human Persona Space。人格来源最终可以参考授权/公开的人类行为资料，但不能简单克隆具体个人。

## 5. 人物不是围绕用户运行的

用户输入只是人物世界中的一种 Event。

人物也会：

- 过自己的日常。
- 有当前关注点和未完成想法。
- 在没有用户消息时经历时间。
- 有理由时主动分享或联系。
- 没有理由时什么也不做。

因此 V0 就保留 World Time、Life Event、Diary 和 Proactive Intent，而不是等做完 chatbot 后再补“生活感”。

## 6. 产品阶段

### Phase 1 — Prove the Person

文本优先。验证 Persona、Memory、Recall、Mental State、Action Choice、长期一致性。

### Phase 2 — Prove the Society

再加入 Profile、Feed、评论、DM、发现与重逢、动态推荐，让不同人物真正构成社会关系空间。

### Phase 3 — Presence

再增加 Voice、Full-duplex、Avatar/Video 等存在感能力。

当前仓库只服务 Phase 1。
