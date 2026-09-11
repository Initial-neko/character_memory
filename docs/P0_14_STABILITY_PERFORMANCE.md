# P0.14 — Stability + Performance Baseline

P0.14 不扩展新的社交能力，先收敛 P0.5～P0.13 快速迭代积累的稳定性和延迟问题。

## 目标

1. SQLite schema 演进有可追踪、幂等、可重试的 migration 账本。
2. 不同人物的模型请求不再被一个全局模型锁串行化。
3. 减少聊天完成后的重复 API 请求、重复 DOM 重绘和后台轮询。
4. 降低 Memory Recall / Admission 的无效 CPU 与数据库扫描。
5. 保留现有 Persona、Memory、群聊因果顺序和 Trace contract，不用“并行化一切”换取表面速度。

## SQLite Migration Ledger

新增 `schema_migrations(name, applied_at)`。

当前核心迁移：

```text
core/001-intent-source-event
core/002-epoch-time-keys
core/003-mental-state-history
core/004-runtime-trace-extraction
core/005-indexes
```

群聊迁移：

```text
group/001-epoch-time-keys
group/002-indexes
```

迁移 callback 和 marker 在同一个 Store transaction 边界内执行。失败时不会写入 marker，下次启动可以重试。

旧 `ACTION.metadata.trace` 保留轻量兼容清扫，因为旧写入端可能在 migration marker 已存在后仍产生 legacy trace；这是数据兼容逻辑，不再承担 schema 变更职责。

## Provider Concurrency

P0.13 已经把正式 Trace 改为每次调用返回独立 `ModelCallTrace`。因此 P0.14 移除包住整个 HTTP 模型调用的 model-wide lock。

新的并发边界：

```text
same character turn -> ChatService character lock -> serialized
same group turn     -> group turn lock -> causal order preserved
different character/conversation -> provider HTTP calls may overlap
```

`last_request_messages / last_response_text / last_attempt / last_model` 只是兼容调试字段，只用窄锁更新，不再决定 Runtime persistence。

## Memory Performance

### Recall

仍保持当前完整 Memory baseline 和原有分数：

```text
0.70 semantic + 0.20 recency + 0.10 importance
```

但 cosine / score 改为 NumPy 批量计算，减少 Python per-memory scoring loop。

### Admission

大部分普通轮次模型会返回：

```json
{"memory_candidates": []}
```

此时不再为了 duplicate check 第二次读取该人物全部 Memory。

长期 Memory 数量继续增长后，`list_memories -> in-process vector scan` 仍会成为 O(N) 瓶颈；P0.14 先测量，不在本轮引入 pgvector / 向量数据库。

## Web Latency

### Direct chat

成功的 `POST /v1/chat` 已经返回这一轮 authoritative actions。前端保留 optimistic user message + response actions，不再成功后立即额外 GET 最近 180 条历史并全量重绘。

失败时仍重新读取历史，以处理“服务端已提交、客户端连接随后失败”的边界。

### Group chat

`POST /v1/groups/{id}/chat` 本身已经返回完整 authoritative messages。成功后不再立即重复请求 `/history`。

群聊成员模型反应仍按顺序执行。这是为了让后一个人物能够看到前一个人物在同一 turn 的公开动作，不能简单并行化。后续 UX 优化应优先考虑 progressive delivery，而不是破坏群聊因果顺序。

### Chat rhythm

第二/第三个 Action 仍保留自然停顿，但单次人为等待上限由约 1.2s 收紧到 0.6s；媒体/表情间隔同步缩短。

### Unread polling

Character summaries 从 5 秒一次降低为 10 秒一次；页面处于后台时停止 interval 请求，重新可见时立即刷新。

## 仍需重点观测的性能项

按优先级：

1. `model_ms`：通常仍是单聊端到端延迟最大项。
2. `recall_ms`：长期 Memory 数量增大后的 O(N) 扫描。
3. 群聊 total latency：2～4 次模型调用按因果顺序累计。
4. Sticker/Image catalog：部分 API 仍会重新读取 manifest / 检查文件存在性，可在真实数据量增加后增加 mtime-aware cache。
5. 180 条历史全量渲染：切换会话仍合理，但未来可改游标分页/增量追加。
6. 首次 Runtime 初始化：本地 SentenceTransformer 首次加载是冷启动成本，不应和 steady-state chat latency 混为一谈。

当前已有的 `runtime.timings` / API timings / browser console timings 应先用于确认瓶颈，再决定后续结构升级。

## Regression Gates

P0.14 新增测试确保：

- 不同 session 的共享 Provider 请求可以真实 overlap；
- `memory_candidates=[]` 时不会读取全部长期 Memory；
- schema migration callback 只执行一次；
- Direct / Group 成功发送后不做重复 history reload；
- 后台 summary polling 被节流；
- chat rhythm 不重新回到 1.2 秒级人为延迟。
