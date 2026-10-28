# P15 全栈验收与「五日故事」证据

> 类型：一次性验收证据（acceptance evidence），不是产品契约，也不改变 `docs/current/` 中的行为承诺。
> 验收对象：`origin/main` @ `6439f5ea81be85364d951e6822e2d1104882f1ff`（2026-10-28 23:04 +08）。
> 最终接受仍属项目用户；本文件不代签，也不把 API/CI 绿灯等同于语义或视觉通过。

## 0. 结论摘要

| 项目 | 结论 | 说明 |
| --- | --- | --- |
| 独立启动与版本 | PASS | `:8011` 独立实例；模型、临时 DB、23 个人物正确；产品 `:8000` 未触碰 |
| 备份与恢复 | PASS | SQLite 在线备份 + `integrity_check=ok`；恢复实例 `:8012` 读回历史与故事 |
| 完整故事（加速） | PASS | 偏好 → 真实聊天 → 建议选择/编辑/发送 → 明确反馈 → 下一轮变化 → 2–3 伙伴情境 → 分支 → 结束 → 复盘 → 显式采用 → 重启仍在 |
| 跨日（受控时钟） | PASS（受控） | 两次调用相隔 1 天，Trace 显示「距离上次聊天：1 天」；**不是真实多日观察** |
| 语音 | PARTIAL | Provider/模板注册可读（10 个模板）；Media Runtime `:8001` 未运行，TTS 合成未执行 |
| 形象 / 头像 | PASS | `momo` 头像 JSON + 1,699,844 字节 `image/png` 实际资产 |
| 多模态 | PASS | 真实头像作为用户图片输入，Vision 回合返回结构化 MESSAGE |
| 独立评估（B1–B7） | 未语义通过 | offline：7/7 contract、1 passed、6 unscored；live：6/7 contract、1 passed、5 unscored、1 engineering；无独立评审，**不得称为语义通过** |
| 真实 provider 调用 | 28 次 / 上限 30 | 全部 `SUCCESS`；模型 `deepseek-v4.1-flash`；账本见证据索引 |
| 真实五日观察 | **未执行** | 本轮只做加速故事 + 受控时钟；历史库中虽有多周真实数据，但不是本轮观察 |

## 1. 隔离、环境与未触碰项

- 工作树：`C:/Users/cute/projects/cm-p15`，分支 `acceptance/p15-story`，从 `origin/main` 创建。
- 产品实例 `127.0.0.1:8000`：启动前 PID `50196`；验收全程未停止、未重启、未写入其数据库。
- 验收实例 `127.0.0.1:8011`；恢复验证实例 `127.0.0.1:8012`；两者均在验收结束后停止。
- DB：用 Python `sqlite3.Connection.backup()` 从 `data/character-memory.db` 只读复制到
  `C:/Users/cute/projects/character_memory/.tmp-p15/db/character-memory.db`（664,633,344 字节，`integrity_check=ok`）。
- 配置与密钥：复制 `config.yaml` 与 `.env` 到 `.tmp-p15/`；通过 `CHARACTER_MEMORY_DB_PATH`
  只改 DB 路径。为避免后台自主任务产生不可控调用，验收配置关闭了
  proactive、Space、World、RSS、Group autonomy、Encounter、周期性视觉观察与自主生图。
- 源码：`PYTHONPATH=C:/Users/cute/projects/cm-p15/src`，确认实际导入文件来自工作树；
  评估报告 metadata 记录 `commit=6439f5ea...`、`dirty=false`。
- 环境变量：`HF_HOME=models/huggingface`，`TMP/TEMP/TMPDIR=.tmp-service`，Python 为仓库 `.venv`。
- 用户环境中 `personas/rin/archived.yaml` 将 `rin` 标记为归档，因此 Direct 投递按契约返回
  `DeliveryUnavailable: BLOCKED`；本轮故事改用未归档的 `momo`。这不是产品缺陷。

启动命令（实际执行，节选）：

```bash
export CHARACTER_MEMORY_CONFIG=.../.tmp-p15/config.yaml
export CHARACTER_MEMORY_DB_PATH=.../.tmp-p15/db/character-memory.db
export CHARACTER_MEMORY_EAGER_WARMUP=1
export PYTHONPATH=C:/Users/cute/projects/cm-p15/src
python -m uvicorn character_memory.server:app --host 127.0.0.1 --port 8011
```

## 2. 启动与版本

`GET /health`（runtime 预热完成后）：

```json
{"ok": true, "runtime_loaded": true, "runtime_error": null,
 "model": "deepseek-v4.1-flash", "vision_model": "",
 "embedding_provider": "sentence-transformers",
 "db_path": ".../.tmp-p15/db/character-memory.db",
 "characters": 23}
```

- 模型：`deepseek-v4.1-flash`；Vision 空表示复用 chat model。
- DB：临时副本，未指向 `data/character-memory.db`。
- 人物数：23（来自当前工作区 `personas/*/persona.yaml`，与 `/v1/characters` 一致）。
- 结论：PASS。证据：`evidence/story_steps.json` 的 `health` 步骤；`logs/server.log`。

## 3. 备份与恢复

1. 对验收副本执行 SQLite 在线备份到 `.tmp-p15/db/restored.db`。
2. `PRAGMA integrity_check` → `ok`。
3. 计数一致：`events=32667`、`conversation_events=5181`、`memories=3940`、
   `activities=1`、`scenario_events=12`、`scenario_branches=1`、`scenario_reviews=1`、
   `scenario_adoptions=1`。
4. 以 `:8012` 启动恢复实例（同一源码、同一配置，仅 DB 指向 `restored.db`）：
   - `/health` 正常；
   - `/v1/chat/history?character_id=momo&limit=40` 返回 40 条，其中 8 条为本轮 P15 消息；
   - `/v2/scenarios/{id}` 返回 `ENDED`、6 条主记录事件、`adoption=true`；
   - 恢复读取期间 `llm_calls` 计数不变（0 次新增模型调用）。
5. 结论：PASS。证据：`evidence/backup_restore.json`、`evidence/restart_persistence.json`。

## 4. 完整故事（加速）

一次连续会话，角色 `momo`，会话 `p15-accept-momo2`。所有事件均为真实 HTTP 调用；
建议/聊天/情境/分支/复盘使用真实 provider。

| 步骤 | 实际结果 | 关键证据 |
| --- | --- | --- |
| 选偏好 | Profile `v7 {}` → `v8 {tone:NATURAL, energy:QUIET, verbosity:BRIEF}` | `profile_patch` |
| 真实聊天 | 用户消息 `32625`，人物两条 `MESSAGE`（同轮） | `chat_turn1_history` |
| 建议生成 A | batch `01268a96...`，5 条草稿，`profile_version=8` | `suggestions_A_read` |
| 选择并发送 | 发送草稿原文，服务端归类 `SELECT`；人物回复 | `chat_select_accept` 的 `reply_suggestion_signal.kind=SELECT` |
| 生成 B + 编辑发送 | 因会话推进，旧批次按契约变 STALE；为新事实重新生成 batch `fa985590...`，编辑后发送，服务端归类 `EDIT` | `suggestions_B_read`、`chat_edit_accept` 的 `kind=EDIT` |
| 明确反馈 | `EXPLICIT_FEEDBACK` + `verbosity=BALANCED`，Profile `v8 → v9` | `feedback_explicit`、`profile_after_feedback` |
| 下一轮变化 | 新批次 `4fee0380...` 在 `profile_version=9` 下生成，草稿文本与 A 明显不同 | `suggestions_C_read` |
| 关系情境 | Activity `4a1042a5...`；`momo` + 模拟伙伴 `sim:xiaoyu`、`sim:alei`；`CREATED → ACTIVE` | `scenario_create`、`activity_resume` |
| 练习回合 | 1 位正式人物视角 + 2 位模拟人物视角/回复；全部 `SIMULATED` scope | `scenario_turn1`、`scenario_final_read` |
| 分支 | 从主记录用户事件分叉，分支 `cf40897c...` `READY`，含独立 consequence | `scenario_branch` |
| 结束 | Activity `ENDED`，version 3 | `activity_end` |
| 复盘 | review `READY`，明确区分「实际练习」与「想象分支」 | `scenario_review_read` |
| 显式采用 | 写 1 条 REAL `PRACTICE_ADOPTED` Event `32665` + 1 条 REAL `SHARED` Memory `3940`，同一回执 | `scenario_adopt` |
| 采用进入真实认知 | 下一次真实聊天召回 Memory `3940`（`memory_type=SHARED`） | `multimodal_image_chat.recalled_memories` |
| 刷新 / 重启 | 重启 `:8011` 后 Activity `ENDED`、6 条主事件、分支 `READY`、review `READY`、adoption 一致、Profile `v9` | `evidence/restart_persistence.json` |

边界核对：

- 采用前，练习内容只存在 `scenario_events`，每行 `reality=SIMULATED` 且带 `activity_id`；
  分支事件额外带 `branch_id`。
- 采用后，只有 Event `32665` 与 Memory `3940` 为 `REAL`；`metadata.source` 指向
  `activity_id/branch_id/adoption_request_id/review_request_id`。
- 真实 Direct/Group 召回在采用前看不到练习内容；采用后下一次真实聊天可召回 `3940`。
- 结论：PASS（工程与可观察契约）。语义质量未由独立评审判定。

## 5. 跨日：受控时钟 vs 真实多日

### 5.1 受控时钟（本轮实际执行）

- 角色：`character-c6857f95`（其时间线在本轮前没有未来事件）。
- 第 1 次：`POST /v1/chat`，`at=2026-10-28T23:44:40+08:00`。
- 第 2 次：`POST /v1/chat`，`at=2026-10-29T23:44:40+08:00`，同一 conversation。
- 第 2 次 Trace 的 `# Relationship Time`：

```text
- 上次聊天时间：2026-10-28T23:44:40+08:00
- 距离上次聊天：1 天
```

- 第 2 次回复引用「昨天」并延续前一天的话题（汤/睡眠），说明间隔进入上下文。
- 证据：`evidence/cross_day_controlled_clock.json`（含事件时间、actions、Trace 行）。

### 5.2 真实多日（明确未观察）

- 本轮**没有**连续多日真实陪伴、真实跨日回访或多日人格稳定性观察。
- 副本中确实存在多周历史（例如 `momo` 730 条 Direct 消息、36 个不同日期；
  `character-c6857f95` 341 条、27 个日期），但这是用户既有数据，不是本轮产生的观察。
- 证据：`evidence/history_span_momo.json`、`evidence/history_span_character-c6857f95.json`。
- 因此「五日故事」在本报告中只能表述为：**故事链路已在一轮加速会话中跑通 + 一个受控
  隔日回访**；不能表述为「已观察真实五日」。

## 6. 既有能力回归

| 能力 | 可执行检查 | 结果 |
| --- | --- | --- |
| 语音 | `GET /v1/voice-templates` | 10 个模板可读；`GET http://127.0.0.1:8001/health` 不可达（`URLError`），TTS 合成未执行 |
| 形象 / 头像 | `GET /v1/characters/momo/avatar` + `/asset` | PASS；`image/png`，1,699,844 字节；资产已保存 |
| 多模态 | `POST /v1/chat` 携带真实头像 data URL | PASS；Vision 回合返回 2 条 MESSAGE，内容基于图像（暖米色、发尾蓝色等） |
| 文本/建议/反馈 | 见第 4 节 | PASS |
| Activity/分支/复盘/采用 | 见第 4 节 | PASS |

证据：`evidence/voice_avatar_multimodal.json`、`evidence/momo-avatar-asset.bin`、
`evidence/story_steps.json`。

## 7. 独立评估（P14 runner）

使用 `main` 上的 `character-memory eval`（P14 `feat/eval-trends` 已合入）：

```bash
# offline
python -m character_memory.cli eval .../evals/b1_b7.jsonl \
  --label p15-offline-wt --report .../eval_b1_b7_offline_wt.json
# live, hard cap 7
python -m character_memory.cli --config .../.tmp-p15/config.yaml \
  eval .../evals/b1_b7.jsonl --live --max-paid-calls 7 \
  --label p15-live --report .../eval_b1_b7_live.json
```

| 运行 | 模型 | paid calls | passed | unscored | engineering | contract_passed | 退出码 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| offline | `offline-deterministic-v1` | 0 | 1 | 6 | 0 | 7/7 | 1 |
| live | `deepseek-v4.1-flash` | 7 | 1 | 5 | 1 | 6/7 | 1 |

- offline 只证明 runner/上下文/报告链路可执行，不代表模型质量。
- live 的 `unscored=5` 表示契约通过但**语义评审未进行**；`passed=1` 是 B7 系统契约。
- live 的 `engineering=1` 是 `B1_dialogue_casual`：契约只允许 `MESSAGE`，模型返回
  `["MESSAGE","STICKER"]`，`allowed_actions` 检查失败。记录为工程/行为失败，不是语义通过。
- 没有创建评审 artifact，因此**不得**把 `contract_passed=6` 描述成语义通过。
- 证据：`evidence/eval_b1_b7_offline_wt.json`、`evidence/eval_b1_b7_live.json`。

## 8. Provider 调用账本

- 总数：**28**（故事与受控跨日 21 + live eval 7），上限 30。
- 模型：全部 `deepseek-v4.1-flash`；故事调用状态全部 `SUCCESS`。
- Token：故事 93,367；live eval 24,639；合计 118,006。
- 逐条故事调用含 `request_id`、feature/purpose、输入/输出/总 token：
  `evidence/provider_calls.json`。
- 观测限制：`llm_calls` 表**不持久化 `finish_reason`**，eval 报告也不暴露 provider
  request id；因此只能记录 `status=SUCCESS` + 结构化 schema 校验通过，不能声称已核对
  每次的 `finish_reason`。这本身是观测缺口，见「发现的问题 P15-F2」。
- 失败/重试：两次失败的 UI 前置尝试（归档角色、批次归属）也产生了真实调用，已计入。
  每次调用之后均停下核对，未出现连续失控调用。

## 9. 风险清单核对

来源：`docs/current/DELIVERY_PLAN.md`、`STATUS.md`、`REFLECTION_WATERMARK.md`、
`CONVERSATION_FACT_MIGRATION.md`、`TECH_DEBT.md`、`ACTIVITY.md`、`DSH_AUTHORIZATION.md`。
本轮只标注「已验证 / 未验证」，不改写这些 contract。

| 风险 / 待验收项 | 来源 | 本轮结论 |
| --- | --- | --- |
| P08 日/周/月反思与证据水位 | REFLECTION_WATERMARK | **未验证**：文档明确 `DESIGN FROZEN — NOT EXECUTED`，main 无表/API/scheduler；本轮「下一轮变化」由偏好+反馈实现，不是反思 |
| Direct 对话事实正式 cutover | CONVERSATION_FACT_MIGRATION | **部分**：UNIFIED 读写、历史、建议、采用链路已验证；正式 `--cutover` 未执行，未验证 |
| Screen-share 可读性 | DELIVERY_PLAN 2.2 | **未验证**：无真实桌面共享 |
| 周期性屏幕观察成本/去重 | DELIVERY_PLAN 2.2 | **未验证**：本轮关闭，无观测 |
| Streaming ASR 目标机矩阵 | DELIVERY_PLAN 2.1 | **未验证**：未接 Media Runtime |
| Cost-soak 分项 | DELIVERY_PLAN 3 | **未验证**：本轮主动关闭后台自主任务，样本不具 soak 意义 |
| 真实 Chromium 产品/视觉验收 | DELIVERY_PLAN 4、STATUS #121 | **未验证**：本轮为 HTTP/API 级，无浏览器、无截图、无人工视觉签收 |
| 长期 Person/Society、七日连续性 | STATUS backlog | **未验证**：无真实多日观察；加速故事不能替代 |
| Activity 失败路径（FAILED round/branch/review） | ACTIVITY §7.3/§8.3/§8.4 | **未验证**：只跑 happy path |
| Activity 分支/复盘/采用 scope 边界 | ACTIVITY §8.5/§8.6 | **已验证**：SIMULATED rows、1 条 REAL Event + 1 条 REAL Memory、下一轮真实召回 |
| 读取/重启不调用模型 | ACTIVITY §5/§8.7 | **已验证**：重启与恢复实例读取期间 `llm_calls` 不变 |
| DSH 授权、独立 listener、审计 | DSH_AUTHORIZATION | **未验证**：未启动 `dsh_server`、未注册 subject |
| Voice/TTS 选择、GSV/VoiceDesign、语音消息 | VOICE_AND_TTS | **部分**：模板注册可读；`:8001` 未运行，合成/路由未验证 |
| Settings 持久化 vs runtime 生效 | SETTINGS_CENTER | **未验证** |
| Character onboarding（Persona+Avatar+Voice 首聊） | STATUS P0 | **未验证** |
| 失败恢复（麦克风/摄像头拒绝、SSE 断线、TTS 失败） | STATUS P0 | **未验证** |
| Android 真机 | STATUS | **未验证**，非本轮范围 |
| 无 `uv.lock` 的依赖复现风险 | TECH_DEBT | **已确认存在**；本轮使用现有 `.venv`，未验证锁定构建 |
| 渠道专属 cognition 与共享 PersonContext 漂移 | TECH_DEBT | 非本轮可测项；未验证 |

## 10. 发现的问题（本 PR 不修）

### P15-F1（中）未来时间用户事实可让投递批次卡在 SUPERSEDED，并触发重试风暴

- 触发：同一 Direct 会话中已存在一条 `event_time` 在未来的用户事实（受控时钟
  `at=next day` 或系统时钟漂移/导入数据），随后再发送真实时钟消息。
- 现象：新消息被视为 stale；`discard_stale` 把覆盖两个 pending source 的共享 batch 置为
  `SUPERSEDED`，但 sources 仍是 `PENDING`。`claim()` 对 SUPERSEDED 不复用也不清理，
  于是每次调度都抛 `DeliveryUnavailable: SUPERSEDED`。
- 规模：在 23:28:06–23:32:58 的约 5 分钟内记录 **389 条**同一 traceback（约 1.2 条/秒），
  且人物永不回复。
- 证据：`evidence/finding_f1.json`、`evidence/finding_f1_future_time_delivery.log`。
- 注意：验收脚本为继续后续步骤，事后把这两个 source 手工标为 `SUPERSEDED`；原始观测为
  `PENDING` + 共享 batch `SUPERSEDED`。
- 未修复；建议后续 PR 处理 `claim()` 对 SUPERSEDED 的重claim/清理语义，并加回归测试。

### P15-F2（低）调用遥测缺少 `finish_reason`，eval 报告缺少 provider request id

- `PRAGMA table_info(llm_calls)` 无 `finish_reason` 列；eval 报告只有 per-case token。
- 影响：验收无法逐条核对 provider 的 `finish_reason`，只能记录 `SUCCESS` 与 schema 校验。
- 证据：`evidence/provider_calls.json`。

### P15-F3（低）live B1 契约失败：不允许 STICKER 的用例返回了 STICKER

- `B1_dialogue_casual`：`allowed_actions` 失败，`unexpected=['STICKER']`。
- 属模型行为/契约失败（engineering），需要后续决定是收紧模型行为还是放宽用例。
- 证据：`evidence/eval_b1_b7_live.json`。

### P15-F4（环境，不计缺陷）语音合成无法执行

- `:8001` Media Runtime 未运行；本轮只验证了模板注册，未验证正式 TTS 路由与语音消息。
- 证据：`evidence/voice_avatar_multimodal.json`。

### P15-F5（文档清晰度）场景描述键必须使用 `sim:` 参与者键

- 首次 `POST /v2/scenarios` 用 `local_id`（`xiaoyu`）作为 `descriptions` 键返回 422；
  必须使用 `participant_id`（`sim:xiaoyu`）。契约写的是「participant keys」，实现正确，
  但对调用方不够直观。
- 证据：`evidence/attempt2_momo_attribution_issues.json`。

### P15-F6（范围限制）真实五日未观察

- 本轮没有真实连续五日数据；任何「五日故事已通过」的表述都不成立。受控时钟只能证明
  间隔进入上下文，不能证明长期人格/关系稳定性。

## 11. 证据索引

所有证据在 `C:/Users/cute/projects/character_memory/.tmp-p15/`（本地未跟踪，不提交）：

| 路径 | 内容 |
| --- | --- |
| `evidence/story_steps.json` | 故事全部步骤、HTTP 状态、响应、耗时、paid 标记 |
| `evidence/story_meta.json` | 会话、Activity、branch、batch、profile 版本 |
| `evidence/restart_persistence.json` | 重启后 Activity/分支/复盘/采用/历史/DB 计数 |
| `evidence/backup_restore.json` | 备份、integrity、计数、恢复实例读取结果 |
| `evidence/cross_day_controlled_clock.json` | 受控 1 天回访：事件时间、回复、Relationship Time |
| `evidence/eval_b1_b7_offline_wt.json` | offline B1–B7 报告（commit 6439f5ea，dirty=false） |
| `evidence/eval_b1_b7_live.json` | live B1–B7 报告（7 paid calls） |
| `evidence/provider_calls.json` | 28 次真实调用的逐条账本与总额 |
| `evidence/voice_avatar_multimodal.json` | 语音模板、头像资产、Media Runtime 可达性 |
| `evidence/momo-avatar-asset.bin` | 实际头像字节（1,699,844 B，image/png） |
| `evidence/history_span_*.json` | 既有历史跨度（多周真实数据，非本轮观察） |
| `evidence/finding_f1.json` / `.log` | 未来时间投递卡死与重试风暴 |
| `evidence/attempt1_rin_archived_blocked.json` | 归档角色 `BLOCKED` 的预期行为 |
| `evidence/attempt2_momo_attribution_issues.json` | 场景描述键 422、建议批次 stale 409 的前置尝试 |
| `logs/server.log` / `server-restart.log` / `server-restored.log` | 启动、投递错误、重启与恢复日志 |

## 12. 需要用户决定

1. 是否接受本轮「加速故事 + 受控隔日」作为 P15 的工程验收，而把真实五日观察列为
   单独 gate（本报告不代签）。
2. 是否优先修 P15-F1（未来时间导致的 SUPERSEDED 卡死/重试风暴）再开始真实多日观察；
   该缺陷在时钟漂移或导入未来数据时会影响真实聊天。
3. 是否安排真实 Chromium/视觉验收、Media Runtime 语音合成、Settings/onboarding/
   failure-recovery 与真实多日/七日连续性验收；这些在本轮均为未验证。
4. 是否接受 B1 live contract failure（STICKER）作为后续模型行为/用例修订项。
