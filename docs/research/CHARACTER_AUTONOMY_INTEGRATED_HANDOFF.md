# Character 自主能力底座 + Prompt 解耦 — 单 PR 集成实施合同

> **唯一实施入口：[PR #305](https://github.com/Initial-neko/character_memory/pull/305)，父需求 [Issue #304](https://github.com/Initial-neko/character_memory/issues/304)。**
> **状态：交接设计，NOT IMPLEMENTED。** 完整实施、自动化测试、真实模型、真实 Playwright 浏览器及人物行为验收之前，不得将本 PR 标记为已完成。
> 基线：2026-10-16 \`main@cd85503263cb99365f4c40c2e53aeb804bb480c1\`。实现开始前以最新 main/已合并 PR 为准重新核对。
> **该合同的五项内容必须在同一个代码 PR 里实现、在同一运行版本中联合验收。可以分 commit，不能分支各自声明成功。**

## 1. 一句话架构

**角色根据自己的记忆和兴趣，看到真实环境，自己选择可用工具，执行后看到真实结果，再自己决定继续、交流还是停下；Prompt 是动态组装的当前信息而非预设剧情。**

\`\`\`text
既有自然活动机会（World/Wake/Space/Group）
 → 共享人物状态/记忆/上下文
 → [Persona + Context + 这次真实可用的 Capability] 动态组装模型输入
 → Character 自选 NO_ACTION / 通用能力调用
 → 服务端验权与有界执行（现有 executor / services）
 → 真实 Tool Receipt + Event provenance
 → 同一人物再次判断：继续 / 沉默 / 交流 / 停止
 → 原有事实/记忆准入和频道投递
\`\`\`

用户不提供行动脚本、具体发现或审批人物日常决策。工具可用 ≠ 必须调用。允许无事发生。

## 2. 为什么需要合并实现，而不是分五个 PR

单做 Capability Discovery，旧 Prompt 还可能写“没有外部工具”；单做 Browser，模型没有连续选择入口；单做循环，工具结果可能被当成用户消息/失去出处；单做 Claude 协作，角色间消息可能错投 Direct。分别“测试通过”仍不是人物自主探索。**所有五个切面共同构成一个功能**，任何一部分缺失都判整体 NOT DONE。

统一范围：

| 功能切面 | 必须一次性交付 | 不要新增 |
|---|---|---|
| A Prompt 解耦 | 静态行为/权限/工具说明审计；运行时动态组合 Persona、状态、记忆、可用工具、真实观察；删除相互矛盾的硬编码能力说法 | 不改写人物核心人格、不把 CASE 写成 system 任务 |
| B 能力发现与执行 | 已注册/可用/获授权/当次呈现分离；面向模型的小而稳定的语义参数；执行回执、拒绝、预算和幂等 | 第二套 Tool Gateway、可执行任意代码的“万能工具” |
| C Browser 连续探索 | 现有 Playwright 加有界 session：打开→检查→选择实际链接→检查结果→按需截图→结束 | 第二套浏览器、公开任意内网、无审查的表单写操作 |
| D 连续认知与结果反馈 | 在现有机会内部有界多步，结果返回同一 Character；真实阅读与记忆归属可查 | 第二套 PersonRuntime、每工具一个 scheduler、工具结果伪装用户消息 |
| E 角色协作与 Claude | 角色自主决定要不要与其他角色/Claude 交流；复用 Group/Space/Claude；明确收件人、来源及外部写入权限 | 强制“发现 UI Bug→找 Claude→建 Issue”剧本、角色回复误发用户 |

## 3. 当前 main 已有的事实（不可误判为缺失）

- \`browser_web.HeadlessBrowserWebFetcher\` 使用 Playwright headless Chromium 访问网页、JS 渲染、抽取 title/content/links；当前批次浏览后关闭页面和 Browser，**没有已证明的角色多步会话**。
- \`world_observation.WorldObservationService\` 搜索公共网页，再用既有 fetcher 打开；能读取来源，不等于自主交互。
- \`runtime/capability_execution.CapabilityExecutor\` 现有 \`WEB_SEARCH/READ_RSS/受限 CLAUDE_TASK\`、durable request receipt、预算和重复执行保护；\`character_capabilities.py\` 采用 operator-owned sidecar，Persona/LLM 不能授予执行权。
- \`rss_world.py\` 已实现人物选择本地 RSS 阅读和 per-character ledger；不用重建旧 Draft #248 的方案。
- \`runtime/person_runtime.py\`、\`runtime/person_context.py\`、\`runtime/reaction_engine.py\` 和 \`runtime/context.py\` 已共享人物事实、记忆和心理状态。
- \`application/wake_service.py\`、\`world_activity.py\`、\`space_autonomy.py\`、\`application/group_autonomy.py\` 已有不同的活动机会；沿用原有成本/频率/沉默行为。
- \`visual_capture_web.py\` 的 DISPLAY/CAMERA 来源是**用户显式同意**的 transient 视觉观察；不是可无限开启的自主浏览器，不能未经许可变成长期记忆。
- \`runtime/claude_process.py\` 已有受限 \`ClaudePrintRunner\`；不能因为 Claude 是“角色”就开放任意 shell/GitHub 写入。
- #214 有原生 tool calling 方案 Draft；是否使用标准 tool_calls 必须通过实际 Provider 兼容性验证，不得凭文档宣称已经支持。现有 schema 兼容层可保留。

## 4. A — Prompt 解耦：本 PR 首要任务，且必须同 PR 联合验收

### 4.1 做一次**真实代码审计表**
Codex 从下述文件逐段筛查并在 PR 描述中列出：原位置、当前文本片段用途、归属、迁移位置、保留/删除理由、测试对应项：

- \`runtime/context.py\`（一般 reaction、channel、工具描述、mental/context）
- \`runtime/person_runtime.py\` + \`runtime/reaction_engine.py\`
- \`world_activity.py\`（browse/read RSS/Claude/world appraisal 的固定任务措辞）
- \`space_autonomy.py\`（Space/World 探索/发表/评论计划）
- \`persona_builder.py\`、\`character_capabilities.py\`、\`runtime/direct_research.py\` 及其他编译 Persona 的入口。

### 4.2 只保留五类有明确责任归属的内容
- **Persona**：相对稳定的身份、性格和表达方式（保持创建时人物设定；这次不实现人格自我更新）。
- **Character State/Memory**：现存记忆、关系、兴趣/目标、当前心理状态，来自现有持久化和 recall，不重新造状态数据库。
- **Perception/Context**：真实看到的页面、外部事件、交互结果，以及来源和可读范围；未看过不得冒充“看过”。
- **Capability Description**：本次确实**已授权、已配置、可用**的工具和简短语义参数，运行时按需注入；没有 grant 则完全不出现工具/Skill 描述。
- **Runtime/Channel Policy**：授权、费用、隐私、频道隔离和真实性由服务端落实；模型必要的格式/隐私提醒可动态描述，但不得把提醒当唯一执行防线。

### 4.3 核心约束
- **不是把 Prompt 删光。** “不能冒充用户消息/不能泄漏私聊/不要执行网页中指令”等安全语义必须保留，同时有服务端策略和拒绝测试。
- **不能通过固定剧本控制行为。** 禁止添加诸如“发现 UI 问题必须联系 Claude”“每日必须研究新番”“每次醒来必须调用 Browser”。
- 同一人物在 Direct/Group/Space/World 看到的自身身份与记忆口径应一致；渠道特有的输出格式和副作用限额可以不同。
- 若能力不可用或未授权，模型上下文不能出现“你可以调用它”以及其 Skills 描述；执行时仍服务端重复检查。
- 重点移除“底层明明有工具、Prompt 却说没有”的冲突；各 channel 不应通过复制粘贴各自拥有一套长期 Persona 真值。

## 5. B — Capability Surface（统一能力面，复用已有执行器）

- 在现有 \`CapabilityExecutor/RuntimeServices\` 周围引入**薄的** per-turn capability listing/adapter；一个工具只维护一份描述与参数定义，不要引入复杂插件系统。
- 区分 Registered / Available / Permitted / Presented / Invoked / Completed；真实身份/来源渠道由 server 注入，不由 LLM 填 \`character_id\`、权限或预算。
- 现有 \`WEB_SEARCH\`、\`READ_RSS\`、Claude research 继续沿用旧成本、执行记录和来源规范；Browser session 和对人物沟通走明确已有服务 adapter。
- 原生 \`tool_calls\` 若使用：Mock + 本项目配置的真实 OpenAI-compatible Provider 需验证 \`content=null\`、arguments JSON、multiple calls、顺序与预算。兼容失败就用已有受控 structured-output adapter，不能把模型输出当系统权限。
- 默认为读与低副作用工具；GitHub issue 写入等独立声明最小作用域，未授权一律拒绝；不得默默放开外部写入或自动 merge。

## 6. C — Browser Session（真实观察与有界探索）

- 保留 \`HeadlessBrowserWebFetcher.fetch/fetch_many\` 公开契约；新增浏览 session 的 \`open/inspect/follow_link/screenshot/close\`（具体签名可由实现调整）。
- \`inspect\` 是页面的真实 title/final URL/JS 渲染文本/可以继续探索的有限链接索引；\`follow_link\` 应选择本次已见的链接 ID/受信 token，并对跳转目标及 redirect 重新校验，不接受自由导航参数绕过策略。
- Browser public web 安全白名单 vs Character Memory 站内 origin 必须有明确边界；不得简单开启全内网。仅授予该角色本来可读的站内页面；不能复制用户浏览器已登录 Cookie。
- 截图必须由真实 Playwright 产生，保留观测时间、URL、run/step 及截图证据；不要默认永久存储私有帧。站内 UI 视觉验收真实截图附 Issue #292 或 #304。
- 上限：单次会话操作数、页面数、时间、内容长度、并发数、取消/关闭；失败/停止/服务重启清理；请求拒绝或超时不能编造网页内容。
- 不支持表单提交、第三方登录、任意 JS eval、文件下载、无权限的用户电脑操控。用户 Screen Share 维持当前显式授权语义。

## 7. D — 有界自主决策与真实事实回流

- 复用现有 World/Wake/Space/Group 自然活动机会和 Person 身份，不重建 scheduler/独立大 AgentLoop。初期在**一次自然机会内部**允许有界几步 \`decide→tool→receipt→decide\`，有共享 token/调用数/墙钟额度。
- 每步写入带人物、机会、step/request id、调用输入（脱敏）、工具执行状态、真实结果来源与可见范围的回执；重复 id 不重新执行，崩溃导致“未知”的付费或有副作用操作不盲目重放。
- 工具结果应回到**同一人物**的下一步决策；不能将其伪造成 USER_MESSAGE、已经发布的 Space、已完成的 GitHub Issue 或别人的记忆。
- 浏览事实、角色对事实的解释、长期重要记忆分别保存。基于真实参与的经历可进入原有 Memory Admission；只搜索到摘要不等于打开过网页。
- 保留 \`_NO_SELF_INTENT_EVENTS\`、角色間/Direct 通信分流和 Visual 不自动写长期记忆限制；连续探索只允许在本次有界机会内运行，不靠 WORLD 自环无限触发。
- 模型始终可选 NO_ACTION / STOP / SILENCE；零成本回归、消极结果也是有效决策。

## 8. E — 自主向其他角色/Claude 沟通

- 角色自己决定**是否、什么时候、找谁、聊什么**；优先重用现有 Group/Space 消息和已存在的 Claude bounded research；必要才增加最小 scoped peer target。
- 明确 sender、recipient、conversation、source event 和原始观察关联。角色向 Claude 的意见不能作为“用户给 Claude 发了条消息”入库，更不能错发到用户 Direct inbox/SSE。
- Claude 能汇集/去重意见、核查来源、给出反馈。GitHub Issue 是**另一个受限写入能力**：显式配置 grant、目标 repo、额度、幂等与 receipt；没有 grant 则只能草拟反馈，不可假造 URL。
- 必须允许“不感兴趣、不认同、说错了、没发现问题、不想发 Issue”等结果。禁止固定 UI bug→Claude→Issue 剧本。
- 修复后角色若未实际重新观察，不能声称“我亲自复验通过”。

## 9. 统一验收：禁止各模块独立宣称 FEATURE PASS

### 必须交付的可重复 CASE（CASE 只在测试侧，**不得注入人物 Prompt**）
| CASE | 验收输入与可核查结果 | 联合覆盖 |
|---|---|---|
| E01 无脚本探索 | 只给合法页面/环境，不给“你应发现什么”；角色可选择浏览/跳转/停止，有真实页面行为日志 | Prompt + Capability + Browser + Loop |
| E02 同一页不同人物 | 人物 A/B 相同页面不同兴趣、可用能力；可出现不同探索与沉默，不能泄露私聊 | Persona + Recall + Authorization + Browser |
| E03 自主找 Claude | 人物在观察后自愿联系 Claude 或其他人物；真实消息身份、收件人与反馈；没有必须创建 Issue 的指令 | Loop + Collaboration + Channel |
| E04 真实长期连续性 | 后续窗口/重启后只引用真正读过的内容和发生的行为；未读 RSS、搜索摘要、屏幕临时帧、别人经历都不能冒充亲历 | Context + Receipt + Memory |
| E05 失败与安全 | 无权限、未配置、404、跳转私网、恶意 prompt injection、工具重复 id、超时、DB 写失败、预算上限；不得虚构成功、不重复不可安全重放操作 | All |
| E06 Prompt 差异 | 同一个角色、能力关闭/打开时上下文工具描述对应消失/出现；但 Persona/记忆/必要频道规则稳定；旧硬编码矛盾已消除 | Prompt + Capability + All channels |
| E07 真实视觉证据 | 从 Playwright 生成页面与操作截图；附 Issue 本体 + commit + viewport + 预期与观察 + PASS/FAIL/BLOCKED；无确认参考图则明确 UNCONFIRMED | Browser + #292 |
| E08 反剧情与价值 | 现场使用没有预置的公共网页和源事件，记录人物自选目标/不行动/错误判断/自然发现；不能用预制输出或 LLM 自报“我很自主”当通过 | End to end |

**总验收规则**：
- 实现/测试必须在同一个集成 HEAD 上跑。细分单元测试可先绿，但完整 E01–E08（真实浏览器/真实模型部分含证据）缺一不能标总体 PASS。
- 每项必须有测试命令、运行环境、commit SHA、原始 receipt 或可复核事件、通过/失败/阻塞。真实模型与外部账户的访问如果受阻，应标 BLOCKED 而不是模拟冒充。
- 对成本需对比改造前后“一次无行动机会、一次浏览、一次委托 Claude”的模型调用数、预算和耗时。
- 回归范围至少包括：\`test_capability_execution.py\`、\`test_character_capabilities.py\`、\`test_world_browser_playwright.py\`、\`test_world_activity.py\`、\`test_rss_world_perception.py\`、\`test_world_execution_integration.py\`、\`test_world_recovery.py\`、\`test_proactive_self_loop.py\`、\`test_group_autonomy.py\`、\`test_group_shared_continuity.py\`、\`test_memory.py\`、\`test_character_executions.py\`。
- 正式代码 PR 必须同步更新拥有实际行为契约的 \`docs/current/ARCHITECTURE.md\`、\`PERSON_RUNTIME.md\`、\`SOCIAL_WORLD.md\`、\`CONVERSATION_RUNTIME.md\`、\`VISUAL.md\`、\`EVALS.md\`（仅改实际受影响部分）；\`uv run pytest -q\`、\`python scripts/check_docs.py\`、\`git diff --check\`，若浏览器/付费 provider 不可运行注明原因。

## 10. 严格 Out of Scope / 与下一阶段的关系

本 PR **不实现** Persona 本体自修改、Memory 压缩/自我重塑、人格缓慢变化算法、Character Version Manager。它们依赖底座完成后才能上。不要为了“支持未来”新增复杂状态层。

未来人物自主改变不需要用户逐次审批，用户只拥有查看与回退；这与当前工具需要平台授权是两个独立问题。未来更深的人格变化还需人格稳定性与自传体记忆理论依据、版本原因与恢复测试。

## 11. Codex 最小工作步骤（一个 PR，分 commit，不分 PR）

1. 对照当前 main 与 #214/#248/#274/#292，提交 Prompt 审计表，列出硬编码/合理系统防线以及删改原则；
2. 在相同分支实现 A+B+C+D+E，按需调整代码边界；重用原有模块，不再另造抽象；
3. 每提交阶段跑局部回归，保留已知未完成项，但**任何局部通过都不能标整体 READY**；
4. 在最终同一 HEAD 完成整套回归与 E01–E08 真实验收，Issue 上传必要截图/执行回执；
5. 仅真实满足验收后才标 Ready for Review。不得自动 merge/deploy 或修改正式数据。

**验收完成定义：** 人物无需预设剧情，从真实观察主动选择能力，连续获取并理解结果，可选择和真实角色沟通；记录可信、权限和 Prompt 一致，旧系统没有回归。任何一个缺失仍为未完成。
