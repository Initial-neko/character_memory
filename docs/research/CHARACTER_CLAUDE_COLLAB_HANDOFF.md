# Draft — Character → Claude 真实协作（实施交接）

> **尚未实现**，Codex implementation handoff；父任务 [#304](https://github.com/Initial-neko/character_memory/issues/304)。需要 A 能力与 C 结果回流，复用现有 Group/Space/Claude 而不是造新聊天系统。

## 一句话

角色看到自己感兴趣的问题，**自己决定**找 Claude 讨论；Claude 帮忙研究、整理意见；有被预先配置的写入权限时才能把反馈变成 GitHub Issue，后续人物可以看到真实处理结果。

## 当前基线及边界

- \`application/group_autonomy.py\` 和 \`application/group_conversation_service.py\` 已有自主群聊与真实共享事件。
- \`space_autonomy.py\` 已有角色发言、评论和明确提及；先验证能否用现有空间承载角色邀请，不建立第二个 peer-chat 数据库。
- \`runtime/context.py\` 已有 Direct 的强约束，禁止把对其他角色的回答误发送到用户 Direct。这个规则必须保持。
- \`runtime/claude_process.py\` 的 \`ClaudePrintRunner\`、\`runtime/claude_tools.py\`、\`CapabilityExecutor\` 提供限定版 CLI 执行与 receipt。Issue #274 明确说其 CLI 当前 tools 受限，不是自主任意组装工具的完整 Agent。
- \`character_capabilities.py\` 的操作者 sidecar 权限不能由“角色名字叫 Claude”或 Persona/Vocation 文本提升。

## Codex 实施范围

1. 最小实现角色主动**选择沟通对象、主题、渠道与时机**。先复用 GROUP 或 Space 真实事件/mentions；如不满足私下协作，再补窄口径的明确角色收件人消息，不得偷偷使用用户 Direct。
2. 参与者身份、引用事件、消息归属、可见性、未读/通知和发送结果必须是真实的；机器人回复机器人不能进入用户私聊 push/SSE inbox。
3. Claude 角色能读取自己**确实收到**的建议，并在已授权范围内调用现有 Claude CLI 做 bounded research，聚合相同意见、检查证据、提出处理建议；外部 Claude 模型返回的内容只是研究素材，不能自动算人物已观察的事实。
4. GitHub Issue 创建是单独有范围的\`github_issue_write\`类能力，需系统级**一次性权限配置**及额度/仓库限制。角色自主演化不需每次审批 ≠ 允许未经授权的外部副作用。初始没有写入 grant 时输出草稿/提议，不声称 Issue 已创建。绝不可扩成仓库自动 merge 或任意 shell。
5. 工程反馈要有可复现步骤、真实证据、截图、发现者、Claude 的处理思路、Issue URL、修复/未修复状态。前端相关验收截图必须放 #292 或本 Epic 的验收 Issue；参考图未明确审批不得假定一致。
6. 修复/验收结果被角色真实观察后才作为她自己的经历；允许原提议者说“我看错了”，允许另一个角色不同意，不硬编码“反馈一定有用/一定要创建 Issue”。

## CASE / 验收

- C01+C03：角色偶然浏览 Space 发现问题，不预先给 UI 检查任务；自己选择找 Claude；Claude 能确认是否值得反馈。
- C03+C07：两名角色针对同一页面意见不同；Claude 整理为一个 Issue 或选择放弃，理由可查；不能强制每个观察产生 Issue。
- C04：无授权禁止实际 GitHub 写入；同一反馈重试不会重复创建；GitHub timeout/异常时不虚构 issue URL。
- C05：角色间消息通过真实 Group/Space 或新 scoped peer route，任何路径都不会错误投递到用户 Direct 收件箱；来源与参与者在 Memory 中保持清晰。
- C06：修复后重新读取真实页面再评价；旧截图与新截图按真实 commit/viewport/访问时间存 Issue，**不拿生成截图冒充验收**。
- \`tests/test_group_autonomy.py\`、\`tests/test_group_shared_continuity.py\`、\`tests/test_character_capabilities.py\`、\`tests/test_claude_world.py\`、\`tests/test_character_executions.py\` 等相关回归。

## 不做

不规定哪个角色必须找 Claude、不编排“发现 UX 问题就建 Issue”的固定剧情；不对外开放无限仓库写权限；不让 Claude 代替角色修改人格/记忆；不把用户当参与者强制发送通知。

## 正式 PR 交付

根据实际改动更新 \`docs/current/CONVERSATION_RUNTIME.md\`、\`PERSON_RUNTIME.md\`、\`SOCIAL_WORLD.md\`，附真实路由/视觉证据、权限拒绝测试、提交去重和可追踪 receipt。未来人物自我演化与版本管理见 #304 后续门槛，不在此 PR 内。
