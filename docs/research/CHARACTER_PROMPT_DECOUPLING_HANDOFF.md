# Draft — 人物 Prompt 解耦（Codex 交接与验收）

> **独立 PR，和 #305–#308 同时开发；不得合成一个巨型 PR。**
> Parent Issue [#304](https://github.com/Initial-neko/character_memory/issues/304)；2026-10-16 审查基线 main@cd855032。**当前只有设计稿，NOT IMPLEMENTED。**
> 这份 PR 负责 Prompt **表达与组装**，不改 Browser/CapabilityExecutor/自主演化的业务引擎。

## 一句话目标

**Prompt 只按本次人物真实身份、记忆、情境和已授权能力动态组装；不能再用散落的硬编码规则替人物规划生活。**

## 先审计，不盲删

Codex 必须先检索所有模型输入构建点并提交一张审计表：具体文件/片段、原职责、改造后的唯一来源、是否可删除、保留原因和测试。优先：

- `src/character_memory/runtime/context.py`：`compile_context` 的 Persona、Memory、channel、动作约束、动态资源介绍。
- `src/character_memory/runtime/person_runtime.py` 和 `runtime/reaction_engine.py`：人物事实与 reaction context 的输入。
- `src/character_memory/world_activity.py`：`PersonalBrowsePlan`、RSS/World appraise、Claude research prompt。
- `src/character_memory/space_autonomy.py`：Space/World explore、角色互动与 Scene prompt。
- `src/character_memory/persona_builder.py`、`runtime/direct_research.py`、`runtime/person_context.py`，以及 Group/Visual 特殊 channel 的所有额外提示。

审计结果应明确区分：
1. **Persona（相对稳定的人物设定）**：身份、性格、表达风格；不能因换浏览器自动重写。
2. **State/Memory（动态状态）**：当前心理状态、目标、真实经历、关系和来源；同一人物统一取现有 PersonContextBuilder。
3. **Perception（实际观察）**：刚刚看到的网页、RSS、World/Space 事件、Tool receipt；未打开页面不等于读过正文；外部内容是 untrusted data。
4. **Capabilities（当次真实可用能力）**：按 #305 的当前权限与 availability 注入工具简短语义说明，无权限的工具与 Skill 描述 **完全不可见**。
5. **Runtime Policy（系统硬边界）**：频道权限、隐私、外部操作许可、成本、Schema，由服务端执行；必要模型提示保留，但不是唯一防线。

## 必须修复

- 现有工具与 Prompt 描述不一致：有权限且可用时应展示；不可用/未授权时彻底不注入；能力列表由 #305 的单一来源决定。
- 去掉“固定业务剧本”：不得写“发现 UI 问题必须找 Claude”“每天必须研究动漫”“每次唤醒都必须浏览网页”之类行为。角色可选沉默、停止、另一个目标。
- 防止 Direct/Group/World/Space 不同 prompt 路径让同一角色的 Persona/事实/最近经历产生互相矛盾的版本。Channel specific schema/安全措辞可以不同。
- 将重复的工具/角色身份说明和参数细节从各 prompt 中合并为可复用的编译接口；**不要为抽象而再造通用模板 DSL**。
- 保留：不得冒充他人/已读事实/已完成行动；不从私聊转发给其他角色/World；模型必须把网页作为数据；截图隐私；无需回复的权利。
- 现有 `_NO_SELF_INTENT_EVENTS`、`VISUAL_OBSERVATION` no-Memory 规则与副作用限制是服务端政策；不得为“去硬编码”而拆除。
- 仅构建 Prompt 的层不执行工具、不会因生成上下文而新增外部 API 调用。

## 与其他 4 个独立 PR 的接口与写入边界

- **#305 Capability** 拥有授权/注册/工具描述和本轮可见工具列表的事实真相；本 PR **消费它提供的只读描述**。先在双方 PR Body 共同确认 schema，允许在尚未合并时用 stub/契约测试开发，但不得发布矛盾签名。禁止本 PR 另造 capability registry。
- **#306 Browser** 拥有 Playwright session 的执行和安全；本 PR 仅定义 Browser Observation 如何被安全描述为「自己刚刚实际看到」的信息，不负责 open/follow。
- **#307 Autonomy** 拥有 ToolResult→下一轮自主决策的消费和调度；本 PR 提供与其相容的 context compiler，绝不新造 loop。
- **#308 Collaboration** 拥有角色消息投递及 Claude 权限；本 PR 只负责正确表示频道/角色收件人和已授权工具；不能帮 Claude 虚构 Issue URL。
- 高冲突文件：`world_activity.py` 和 `space_autonomy.py` 目前混有 prompt + execution。对这些文件的 Prompt 片段由本 PR 负责，对行为状态机由 #307/#308 负责；需提前确定函数切面，独立 PR 可以先改独立模块，再在统一测试分支确认合并冲突和语义，不得绕开冲突用重复代码。

## 验收（独立 PR 的局部证明 + #304 的联合 E2E）

- PR-P01：同一人物在相同记忆/状态下，Browser grant 关/开，对应工具介绍在模型输入中消失/出现，Persona/Memory 不变；未授权 Skill 无泄漏。
- PR-P02：Direct/Group/Space/World 统一身份与私有信息边界，必要渠道约束依然准确，违规执行在服务端被拒绝。
- PR-P03：搜索失败、read-only 截图、未打开搜索结果、Tool 未真正执行时，不进入“我亲眼看过/完成了”的第一人称记忆。
- PR-P04：不存在 UI Bug、Claude、动漫、RSS 等领域**固化必做**剧情；每次机会模型允许 NO_ACTION，调用失败时不会编造外部成果。
- PR-P05：无可用 Tool 时、目前已经支持的 chat、group、space、RSS、visual / image-gen 等旧流程不退化；对现有 prompt snapshot 和长时 Persona fidelity 增加回归。
- 无权限、未配置、跨频道测试在 #305–#308 共同环境中复跑；**本 PR 局部 CI 通过不代表 #304 的自主底座完成**。
- 必要静态文本检查 + fake-model context capture + 真实模型输入检查（带脱敏来源）。任何真实提供者无法验收，标 PENDING，不报告通过。

## 代码变更建议

优先修改 `runtime/context.py`、相关上下文构建器及 minimal shared prompt fragment；对 `world_activity.py` / `space_autonomy.py` 先以独立 helper 和最小适配形式迁移具体 prompt 编译，减少与 #307/#308 并行编辑冲突。切勿用“全部换成一个统一 Prompt”破坏 channel-specific 约束。

## 不在本 PR 中

人物自主改变/重写核心人格、Memory Consolidation、Character Version Manager、注册和执行 Browser tools、新建 Claude Agent、额外定时任务、自动外部 Issue 写入。以上有明确 PR/未来阶段归属。

## 同步推进/完成定义

**与 #305–#308 同时启动，五个 PR 各自有可审查的差异与局部测试；但总目标只有全部合并到同一集成 HEAD、#304 E01–E08 通过才能标整体 PASS。** 切记不是一个 PR 做完再开下一个，也不是五个 PR 互相独立宣称成功。

正式功能提交时更新实际修改的 `docs/current/PERSON_RUNTIME.md`、`ARCHITECTURE.md`、`SOCIAL_WORLD.md`、`EVALS.md`；跑 `uv run pytest -q`、`python scripts/check_docs.py`、`git diff --check`。