# Draft — Character 能力发现与执行边界（实施交接）

> **尚未实现。** 这份文档是基于 main@cd855032 的 Codex 实施说明；PR 保持 Draft，不可被当作功能交付。
> 父需求：[自主能力底座 #304](https://github.com/Initial-neko/character_memory/issues/304)。旧工具方案：Draft #214；原生 Tool Calling 不是本 PR 的预定结论。

## 用一句话解释

人物运行时可以查看**这次真正可用的工具**、自主选择调用；现有后端负责权限、参数、预算、结果，而不是把每个行为写死在 Persona/Prompt。

## 已核对基线

- `runtime/capability_execution.py` 已有 `CapabilityRequest/Result`、权限/预算判断、持久化 execution receipt、防重放。
- `character_capabilities.py` 的 `capabilities.yaml` 仅赋予 `claude_research/claude_task`，Skills 为显式选择，不得靠 Persona 获取权限。
- `world_activity.py` 已能在独立 World 机会选择 `WEB_SEARCH/READ_RSS/NO_ACTION`；`runtime/direct_research.py` 承担 Direct 有界 Claude task。
- `runtime/context.py`、Space/World、Direct 对工具的模型提示仍分散且静态。本 PR **不**重写它们。
- Draft #214 讨论了 native `tool_calls`，需做 provider 兼容性实验再决定是否迁移；不得创造“第二套 Agent”。

## Codex 任务（实现时）

1. 设计最小 read-only `available_capabilities(character_id, channel, opportunity, provider_state)` 查询。输出 tool id、摘要、**语义参数**与作用域；可用性同时受配置、channel、role grant 和资源实际状态控制。
2. 调用结果仍交由现有 `CapabilityExecutor` 或对应已有 executor，不能因为暴露新的 Discovery API 绕过准入、幂等、失败回执、隐私和费用限制。授权用户身份从 server runtime 取得，**不是模型填的参数**。
3. 抽一个狭窄的 capability adapter 供后续 Browser/Claude/角色消息挂接。**不要**直接暴露 arbitary HTTP、任意 Python、shell、数据库写入。
4. 对角色没有的能力，从模型输入中完全移除其工具描述和 Skills 描述；显示“被拒绝但仍可调用”不等于禁权。
5. 保持 existing direct/group/space/world 行为不变。不要为了统一表面重写既有 PersonReaction 或全量 Prompt；需要额外 LLM call 时先记录成本与延迟。
6. provider-native 工具协议先写一份 mock + 实际配置 gateway 的验证，明确处理 `content=null`、多个 `tool_calls`、参数 JSON 错误；兼容失败可保持当前 typed-JSON 适配，不要自动扩大工具权限。

## 验收（不得只靠模型自述）

- 未授权的人物没有相应 tool/Skill 的 **描述、可调用性、执行痕迹**；已授权人物只拿到自己允许的能力。
- `DIRECT/GROUP/SPACE/WORLD/VISUAL` 的权限隔离保持；单次模型输出伪造 `character_id/channel/role_grant` 失败。
- 未注册工具、无效/额外参数、缺少 provider、额度用尽、同一 request_id 不同 payload 等全部 fail closed。
- Read-only `list` 不产生 LLM call、数据库副作用或假 Event。
- 本 PR 后续要跑 `tests/test_capability_execution.py`、`tests/test_character_capabilities.py`、`tests/test_world_execution_integration.py` 及新能力发现/拒绝回归，完整 pytest 与文档检查。
- 验收 CASE：#304 C02/C03/C04。无人物 Prompt 的特定“找 UI bug/找 Claude”指令。

## 明确不做

不实现浏览器会话；不实现多步 Agent 循环；不增加新的外部写入权限；不改 Persona、自我更新、Memory Consolidation 或 Version Manager；不做每天强制反思。

## 正式 PR 交付时

将真实契约写入 `docs/current/PERSON_RUNTIME.md`、`ARCHITECTURE.md` 相关章节；测试、实际配置的 provider 兼容性结果、0/新增 LLM 成本、回滚方案放 PR Body。
