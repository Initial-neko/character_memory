# 项目约定（Agent 契约）

用户指令 > 本文件 > 默认行为。

## 开发流程：不要用 SDD

**不要使用 SDD（subagent-driven development）开发本项目。** 即不要做
task-brief 抽取、冻结 diff package、每个任务派一个独立审查代理、维护 progress ledger
那一整套流程。2026-09-21 用户裁决：它把一个小功能拖成了多轮文书往返，速度不可接受。

改用：

1. 直接实现，边写边跑测试。派子代理可以（为了省主上下文），但派的是"实现 + 回报"，
   不是流程本身。
2. 提交小而自洽；`git add` **逐文件点名**，**禁止 `git add -A` / `git add .`**。
   并发提交时用 `git commit -- <路径>` 把路径写在命令上，别依赖共享索引。
3. 需要复核时做**一次**聚焦审查（针对具体风险点），不是每任务一轮、不是多轮修复波。
4. 先出能看/能听的结果，再补周边（文档、脚本收尾）。

## 常设约束（用户历次裁决）

- **不做兼容层。** 旧格式 / 旧配置键不兼容就不兼容：不加回退分支、不加双读者、不加迁移。
  架构完整性优先于向后兼容；旧数据不兼容就响亮报错，不做静默降级。（2026-09-20）
- **音色稳定是第一优先**，高于新功能。
- **`.env` 由用户自己管**，agent 不得修改。测试**绝不能碰真实 `.env`**：任何构造
  `SettingsStore` 并写入的测试必须传 `env_path=<tmp_path>/.env`。
- **跨边界的契约改动必须有真往返测试。** sidecar 之间的 schema 漂移在本仓库静默失败过
  （freeze 写出的文件被 reader 拒绝）：跨进程 / 跨模块 / 写文件再读回来的改动，要用真文件、
  真路径验证，不能只用假对象。
- **删除文件需要用户执行**：`.claude/settings.local.json` 里 `rm` 被 deny。不要绕过
  （不要用 `python os.remove`、不要用 `git clean`），把路径交给用户。

## 提交与回复

- 提交信息末尾加 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。
- PR 描述末尾加 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`。
- 回复用中文且简短（默认 1–5 行）；代码、标识符、提交信息用英文。分析过程留在文件里，
  不要搬进聊天。

## 质量基线

- 全量 `uv run pytest -q` 必须 **0 failed**；skipped 恰为 5 条（Playwright 门控用例）。
- 全量约 20–40 秒；不要为了"跑得快"跳过它。
