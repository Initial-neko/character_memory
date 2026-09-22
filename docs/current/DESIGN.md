# Product Design — Persistent AI Person

本文只记录已经确认的产品原则，不描述具体数据库实现。当前产品验证仍然是 **Prove the Person**；Voice、Avatar、ImageGen 等能力是支持人物存在感和表达的渠道，不代表产品已经跳到另一个目标。

## 1. 我们在做什么

核心不是“一个更会回答问题的 chatbot”，也不是“创建一个永远围着用户转的 AI 伴侣”。

目标是构建 **Persistent AI Person Engine**：

- 同一个人物跨天、跨月仍然可辨认。
- 人物有自己的过去、注意力、观点、边界和未完成事情。
- 关系来自反复相遇和共同经历，而不是用户创建时填写一个关系等级。
- 人物当前的行为应尽可能能被 Persona、Memory、Mental State、关系时间和最近经历解释。
- 用户未来回来，是因为想再次找到“这个人”，而不只是想“使用 AI”。

长期判断标准：30 天之后，人物是否仍然是同一个人，同时又真实地受过去 30 天经历影响。

## 2. 核心优化目标

不要优化：

- 人物有多喜欢用户；
- 一轮发多少消息；
- 对用户请求多快答应；
- 单纯点击、停留时长或互动量。

优先优化：

> **人物的行为有多能被过去解释。**

同一句用户输入，对不同 Persona、不同经历、不同 Mental State 的人物应该可能产生不同结果：回复、追问、反对、发图、发表情、沉默，甚至以后再提。

## 3. 产品价值边界

已经确定：

- 不通过依赖、刺激或无止境互动制造留存。
- AI 可以比真人更有耐心、更善于记忆和倾听，但不能因此变成永远迎合用户的服务角色。
- 人物可以不同意、沉默、改变话题、保持距离，也可以有自己的生活和注意力。
- 主动行为必须来自可以解释的 Intent / Wake / 过去经历，而不是“用户一段时间没打开就催促”。
- 多模态能力首先服务人物表达与共同经历，不应该为了展示模型能力机械触发。
- 推荐和主动行为最终应优化 **Meaningful Relationship Formation**，而不是 CTR、Watch Time 或单纯消息数量。

## 4. 关系如何形成

长期产品循环：

```text
Discover
  → Encounter
  → Talk / Share / Experience
  → Remember selectively
  → Re-encounter
  → Relationship
```

关系不是一个需要人为刷数值的 RPG bar。

真正重要的信号包括：

- User Initiated Character Recall：用户是否主动回来找某个具体人物；
- 共同经历是否会在未来自然影响表达；
- 人物是否会形成自己的判断和后续 Intent；
- 长时间不见后，旧关系是否仍然存在但不会机械复读“好久不见”。

## 5. Persona 的定位

Persona 必须改变**行为选择**，而不只是语气。

它至少应该影响：

- 关注什么；
- 如何理解同一个事件；
- 什么值得记住；
- 是否追问、沉默、主动、保持边界；
- 面对冲突和关系变化时如何行动；
- 如何使用文字、emoji、Sticker、已有图片或生成图像表达自己。

目标不是复制现实人物，而是建立一个可评测的 Human Persona Space。人格来源未来可以参考授权/公开的人类行为资料，但不能简单克隆具体个人。

## 6. 人物不是围绕用户运行的

用户输入只是人物世界中的一种 Event。

人物也可以：

- 有当前 Mental State；
- 有未完成 Intent；
- 经历时间；
- 有理由时主动分享或联系；
- 没有理由时什么也不做。

仓库仍保留 Life / Diary / World Time 研究代码，但当前优先级是稳定现实时间聊天、Memory、Wake/Intent 和长期一致性，不继续扩张 Life Simulation 本身。

## 7. 多模态是“表达方式”，不是第二人格

当前已经有：

- 用户图片 → Vision；
- Voice → ASR → 同一个 PersonRuntime → TTS；
- Character Image / Sticker；
- 角色自主生成 `SELFIE / SCENE`；
- 用户显式 AI 生图工具；
- Avatar Search / Generate。

这些都必须继续复用同一个 Persona / Event / Memory / Mental State / conversation history。

Voice 不是一个新的 Voice Agent，ImageGen 也不是一个独立角色。它们只是同一个人物在不同渠道上的感知与表达能力。

## 8. 图片生成的产品边界

角色自主 ImageGen 和用户工具型 ImageGen 是两件事：

### 角色自主

人物决定“自己现在是否想用一张图表达”。用户说“发自拍”不等于系统必须执行；人物仍可以拒绝、文字回应或沉默。

- `SELFIE`：重点是人物本人，Reference 可用于身份一致性。
- `SCENE`：重点是人物想分享的环境/场景/氛围，人物不必出镜。

### 用户工具

用户明确想“画一张图”时，可以直接调用工具，不需要经过人物是否愿意的行为决策。

生成结果默认先进入图片草稿，像 Ctrl+V 粘贴图片一样，由用户确认后再发进聊天。这样工具试错不会污染聊天历史。

## 9. 产品阶段

### Phase 1 — Prove the Person

验证 Persona、Memory、Recall、Mental State、Action Choice、长期一致性和关系可解释性。

这个核心目标仍然没有结束。Voice / Avatar / ImageGen / World Observation 的评价标准仍然是：**有没有帮助验证同一个人物跨渠道仍然是同一个人**，而不是“功能数量多不多”。

### Phase 1.x — Society foundation（当前已经开始）

Character Space 已经落地 shared Feed、评论、点赞、浏览、角色自主发动态、Audience reaction、作者回复和受限 World Observation。这些是 **Prove the Society 的基础设施与早期实验面**，但还不能等同于“社会关系已经被证明”。

当前要验证的是：人物进入公共空间后，是否仍保持与 Direct/Group 一致的身份、记忆和行为逻辑，而不是因为换了渠道变成另一个 Prompt Agent。

### Phase 2 — Prove the Society

真正进入这一阶段需要验证持续的多人物关系形成，而不只是“有一个 Feed”。候选能力包括 relationship/interest-aware discovery、重逢、DM/Group autonomy、长期关系网络和动态推荐，并需要独立 Eval 证明这些互动不是随机热闹。

### Phase 3 — Presence at scale

进一步完善 full-duplex voice、video/avatar presence、多设备等存在感与产品化能力。

“仓库里已经有某个 Presence 原型”不等于 Phase 3 已经完成。

## 10. 长期判断标准

每次增加能力都应该问：

1. 它是否让人物更可解释，而不是更像万能助手？
2. 它是否保留 Event / Memory provenance？
3. 它是否尊重人物可以不行动？
4. 它是否制造新的、无法控制的 engagement 机制？
5. 失败时是否会破坏已经成立的主聊天链路？
6. 30 天后它是否仍有助于用户认出“这是同一个人”？
