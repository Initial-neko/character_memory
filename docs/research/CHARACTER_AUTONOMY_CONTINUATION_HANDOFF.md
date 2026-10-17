# Draft — 有界连续决策与真实结果回流（实施交接）

> **尚未实现**，Draft PR 仅供 Codex 评审与实施。父需求 [#304](https://github.com/Initial-neko/character_memory/issues/304)。依赖 A 能力发现、B Browser 会话的**契约**，实现时应兼容尚未合并的模块。

## 一句话

给人物一次自然活动机会：自己选择工具，看到**真实结果**，自己判断继续、换工具、还是安静结束；每一步都有真实回执。

## 当前 main 已有的部分

- \`runtime/person_runtime.PersonRuntime.handle(event)\`、\`runtime/reaction_engine.evaluate_reaction\`、\`runtime/person_context.PersonContextBuilder\` 是人物认知主线。
- \`application/wake_service.CharacterWakeService\` 提供周期机会（本地 process 级），\`world_activity.WorldActivityService.browse_character\` 提供独立 World 计划/评估/存储；\`space_autonomy.py\` 有自己的公开发帖机会。
- \`runtime/capability_execution.CapabilityExecutor\` 已确保请求记录后执行、重复调用不重放，结果可持久化。
- \`runtime/context.py\` 现有 \`WORLD_OBSERVATION\` 不自动发送 Direct/Space，也禁止借此创建新的未来 Intent。这个防自环边界要保留；不能为了连续执行将每个 Tool Result 伪装成新的 USER_MESSAGE/TIME_TICK。
- \`VISUAL_OBSERVATION\` 是用户临时共享画面，不得未经同意自动形成长期 Memory 或探索目标。
- 特别注意：\`_NO_SELF_INTENT_EVENTS\` 禁止 WORLD/SPACE/VISUAL 创建 Direct 自环，这不是“缺陷”。

## Codex 任务（实现时）

1. 在**已有一次 World/opportunity**范围内做有界 \`decision -> authorized tool -> receipt -> reconsider/stop\`；最初可以限制单个角色每次机会最多 2-3 个执行步骤（数值为工程安全阈值，可评测调整），有时间/成本/并发总体上限；**禁止**无界循环和空闲时强制额外反思。
2. 对每个 action 记录 \`character_id\`（后端身份）、\`opportunity_id\`、\`step_id\`、输入引用、批准范围、执行状态、实际输出来源/范围、时间戳。跨线程重试/重启必须沿用既有 request-id/receipt 幂等语义，STARTED 未知不能盲目重发外部请求。
3. 新一轮模型只能看到**自己真正执行成功**且被授权阅读的结果。工具失败按失败事实显示，不得由模型把“计划做了”写成“已经做了”；未读 RSS/搜索结果摘要不是已读网页。
4. Person 状态、recent events、recall 与 Memory Admission 继续走现有 \`PersonContextBuilder/PersonRuntime\`；不要为 World/Browser 创建第二个人格/记忆库。长期记忆只在角色自己的真实观察/反思有意义时准入；不要把每个网页全文自动写 Memory。
5. 支持角色无任务、无兴趣、失败时自然停下；不发送用户私聊来求注意，不按“发现 UI 问题→找 Claude”编排规则。下一步由模型基于当前目标、情境与工具结果决定。
6. 将 \`source_event/receipt\` 与人物后续解释分开保存；浏览器外部文本是不可信数据，在所有 continuation steps 都不能变成系统指令。
7. 从已有 \`WorldActivity\` 调度开始，保留频率、预算和用户不在场时沉默的权利；Direct/Group/Space 原有服务不得因为探索循环导致消息重复或互相泄露。

## 必须通过的 E2E CASE

- #304 C01+C02：在两页本地 fixture，模型不是被告知“去点哪个链接”，而是根据看到的页面选择链接；继续探索后基于第二页正文形成判断或沉默。
- #304 C04：断网、timeout、3 步额度耗尽、DB 写入失败、进程重启，都不能重复外部付费或把未知结果当成功。
- #304 C05+C06：同一来源两个角色可以一个继续另一个不感兴趣；后续 recall 只提起真正查看过的内容，不把另一个角色/用户的经历当成自己。
- #304 C08：给合法起点但不给具体 CASE 任务，观察主动探索、选择停止的理由，记录多日变化；不得把模拟文字作为真实浏览证明。
- 现有 \`tests/test_world_activity.py\`、\`tests/test_world_execution_integration.py\`、\`tests/test_world_recovery.py\`、\`tests/test_proactive_self_loop.py\`、\`tests/test_memory.py\`、\`tests/test_group_shared_continuity.py\` 保持绿；新加入 budget/idempotency/receipt+recall tests。
- Live model 是否真的自由行动、能否产生非预设发现，属于独立评估，不能用 schema/unit test 替代。

## 不做

不新增独立 Agent planner/server；不重训人格、不修改 Persona 内容、不自动自我版本化、不每日强制反思；不删除已有 \`_NO_SELF_INTENT_EVENTS\` / channel guard；不默许未授权页面保存和私密数据跨频道传播。

## 正式交付

如果改动共享认知契约，更新 \`docs/current/PERSON_RUNTIME.md\`、\`SOCIAL_WORLD.md\`、\`EVALS.md\`，附调用次数、wall time、失败日志、真实模型测试、与原本行为的对比。
