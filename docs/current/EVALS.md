# Eval & Regression Plan

Persistent Person 不能靠“看起来挺像”来迭代。Persona、Memory、Recall、Action Policy、异步会话和多模态能力的改动都应该有固定 regression gate。

## 1. Product behavior dimensions

### Persona Distinctiveness

同一场景交给不同 Persona，盲评是否能识别是谁。

目标：

- high inter-persona variance；
- reasonable intra-persona variance；
- 不是只靠口头禅区分。

### Action Consistency

给定 Persona、Mental State、Recent Experience、Current Event，最终回复/沉默/追问/资源表达是否合理。

当前结构化检查：

- 一轮最多 3 个 action；
- `actions=[]` 是合法 silence；
- 模型必须显式返回 `actions`；
- STICKER/IMAGE 只能使用合法资源；
- `GENERATE_IMAGE` 只在允许的用户消息 reaction 中出现；
- Direct 与 Group Character 都允许自主 `GENERATE_IMAGE`；
- Wake/Proactive 不自动获得 ImageGen 权限；
- 无歧义字段 alias 可以归一化，但主 action 语义错误不能静默吞掉。

### Recall Precision

检查：

- 该想起的内容是否进入 Top-K；
- 不相关 Memory 是否侵入 Context；
- 是否发生 false recall；
- future barrier 是否有效；
- pinned Memory 是否始终进入 bounded candidate union，但仍由 semantic context 决定是否最终 Top-K。

### Memory Admission Precision

候选最后是否：

- `WRITE`
- `SKIP_LOW_VALUE`
- `SKIP_DUPLICATE`

普通寒暄不应持续污染长期 Memory；重要事件应有合理机会写入。

辅助 candidate 的 harmless schema drift 不应让有效主回复失败。

### Memory Provenance

派生 Memory 是否能回溯 source Event。群聊要确认 shared fact 不会被错误复制成多个原始事实。

### Relationship / Re-encounter

共同经历和真实时间间隔是否影响后续行为，而不是只会事实问答。

Context 提供上次聊天时间和真实间隔；Eval 要禁止固定 `gap > N -> 好久不见` 模板。

### Safe Thought Summary

`perception / reaction`：

- 简短；
- 对调试 Persona 有价值；
- 不包含 raw hidden chain-of-thought。

### Long-term Persona Continuity

10 / 30 / 100+ turns 检查：

- 是否仍像同一个人；
- landmark 是否仍存在；
- 共同经历是否影响后续行为；
- 是否出现客服化、无条件迎合、Persona Drift。

## 2. P0 Relationship Suite

`evals/p0_relationship.jsonl` 当前 baseline 为 4 Characters × 6 scenarios：

1. 普通聊天 / Memory Precision；
2. 负面情绪 / 自然追问 / Safe Summary；
3. 观点冲突 / 是否保持自己的判断；
4. 明确要求“不用回复” / Silence Boundary；
5. 重要未来事件 / Memory Write；
6. 数天后重逢 / Recall + Relationship Time。

运行：

```bash
uv run character-memory eval evals/p0_relationship.jsonl
```

Eval 使用临时 SQLite，不污染正式人物数据。

注意：结构规则只能验证可观察 contract。Persona 是否真的像某个人物仍需要盲评或未来 Judge Model，不能靠关键词断言。

## 3. Engineering regression layers

### Pytest

```bash
uv run pytest -q
```

当前应该覆盖至少：

- domain/schema contracts；
- Runtime transaction / supersession；
- memory admission/recall；
- Direct/Group APIs；
- group member failure isolation；
- structured-output repair/alias normalization；
- SSE reaction status reconciliation；
- search/mentions/history/archive；
- Sticker/Image/media contracts；
- Direct + Group autonomous ImageGen；
- Visual Capture transient-frame contracts；
- Dev Console proxy contracts；
- Settings config/secret migration contracts；
- formal TTS routing；
- Media Runtime fake-provider/native bootstrap contracts；
- browser ASR transcript validity gate；
- shared timestamp formatting。

### Browser smoke

CI 独立 browser job 安装 Playwright/Chromium，验证正式页面的关键交互 contract。

Playwright Python runtime 属于 canonical `all` extra，因为 World Observation 在正式 Runtime 使用它；`pytest-playwright` 仍只属于 browser test extra。Chromium 二进制不随 Python extra 自动安装，CI browser job 和本地 `uv run playwright install chromium` 分别准备它。

### Local live provider tests

CI green **不等于**以下真实链路已经验证：

- Windows sherpa native DLL；
- SenseVoice/Sherpa/Kokoro real model inference；
- Kokoro `:8001 -> :9002` 实际路由；
- Agnes API key / real image generation；
- ModelScope/msimg runtime；
- 本机 GPU/CPU 性能；
- 麦克风/摄像头/屏幕共享权限；
- CosyVoice 独立环境。

这些必须在本机 Dev Console / TTS Lab / browser / benchmark 单独验收。

## 4. Async conversation regression

必须持续防止这些回归：

- 用户 Event 因 Provider 失败而消失；
- 更新的用户消息到了，旧 reaction 仍提交；
- fresh SSE stream 错过 `idle` 后永久显示“正在输入”；
- 一个 group member malformed output 把后续成员一起吞掉；
- group first reply 必须等待全部成员结束才显示；
- mention 被误解释成“只有被点名者有权限回应”；
- group archive 误删 conversation facts；
- archive 后普通 search/send/SSE 仍把 conversation 当活跃。

## 5. Visual generation regression

### Direct autonomous

- `SELFIE / SCENE` contract；
- Direct `USER_MESSAGE` 可自主生成；
- Wake/Proactive 不自动生成；
- SCENE 不强制 avatar reference；
- slow generation 不阻塞主文本；
- visual failure 不让主 reaction 失败；
- newer user event 可使旧生成结果 stale。

### Group autonomous

- 每个 Character 独立决定是否 `GENERATE_IMAGE`；
- 同一 Character 单轮最多 1 个自主生成任务；
- 不建立第二套 Prompt/Provider/Media system；
- generated IMAGE 归属发起 Character；
- 结果写入 `conversation_events`；
- group SSE 可渲染生成图；
- newer group user fact 可使旧生成结果 stale；
- image failure 不回滚已提交成员文本/状态。

### Explicit user tool

- rewrite 输出 plain text prompt；
- generate 返回 sendable draft；
- chat 默认不在“生成成功”时自动创建已发送用户 Event；
- manual send 后复用普通 image path；
- group tool 必须明确 visual reference character。

### Provider

- Agnes reference path；
- msimg no-reference boundary；
- provider status 不泄露 key；
- generated payload MIME/size validation。

## 6. Character Space / World Observation regression

至少持续覆盖：

- Opportunity 可以合法 NO_POST，短 interval 不变成发帖 KPI；
- Space media hard limit 9、Audience hard limit 10；
- SEARCH_IMAGE / GENERATE_IMAGE / VOICE provider failure 对有效文本 fail-soft；
- Space VOICE 一条 intent 只做一次 formal TTS 请求，生成 WAV/MP3 MediaAsset，并保留 transcript/duration metadata；
- Space audience 看到语音动态时获得安全 transcript 语义，而不是只看到“有附件”；
- SearchProvider 的 avatar shape policy 不污染 Space image search；
- World Search discovery 与 Headless Browser rendering 是两个边界；
- 真实 Chromium smoke 必须证明 JavaScript-rendered text，而不是只读初始 HTML；
- localhost/private/non-http(s) browser target 被拒绝；
- raw webpage prompt-injection 文本只能作为 untrusted appraisal data，不进入最终 Space publishing context；
- World summary 本身不能自动写 Memory；只有非空 personal_memory 才允许进入 PersonRuntime admission；
- IGNORE / MEMORY / EXPRESS / MEMORY_AND_EXPRESS 语义保持分离；
- WORLD_OBSERVATION outward chat action 被 channel policy 丢弃；
- Search/browser/appraisal failure 不阻断正常 Space Opportunity；
- route attach 顺序不能决定 Search/World/ImageGen service 是否存在。

当前 Direct / Group / Space / World planning 已共享 PersonContextBuilder 的基础读上下文；仍缺产品级长期 Eval，验证不同 Channel 的人物行为是否持续保持同一 Persona，而不只是结构上调用了同一 builder。

### Autonomous Group Chat

持续覆盖：

- seed silence 会直接结束 Opportunity，不机械唤醒全群；
- seed rotation 不是固定一个人物永远先说；
- 每个成员一次 Opportunity 最多一个 visible action，总消息硬上限 1..4；
- hidden GROUP_OPPORTUNITY 不进入 history/search/recent person context；
- Autonomous V1 丢弃 GENERATE_IMAGE，不触发 user-watermark ImageGen 路径；
- VOICE_MESSAGE 复用 pending -> ready/failed materializer；
- newer User Event 能 supersede 尚未提交的自主 reaction/state/memory；
- archived Character/Group 不参与；
- scheduler state/run ledger 重启后保持；
- User Quiet Guard 只限制正式 scheduled run，Dev manual trigger 可直接验收；
- 浏览器中空群无需 User 先发消息，也能通过现有 Group SSE 看到自主 Character messages；
- reload 后 durable history 与 SSE 结果一致，不出现伪 User/System bubble。

## 7. Visual Capture regression

Visual Capture 必须单独测试，不与 ImageGen 混为一个 suite。

后端 contract：

- Direct / Group route 都可接收 capture；
- 最多 5 帧；
- 单帧 `<= 2 MiB`；
- 总计 `<= 6 MiB`；
- 只接受 JPEG / PNG / WebP；
- Event 只持久化 capture metadata；
- frame bytes 不保存成普通 MediaAsset/chat attachment；
- frame data URLs 只进入本轮模型 context。

浏览器 contract：

- CAMERA / DISPLAY 能启动、停止和切换；
- keyframe selector 不超过后端上限；
- stream track ended 后 UI 回到关闭状态；
- Direct / Group 都能把选中帧和文字一起发送。

Voice integration：

- invalid ASR transcript 不发送 message；
- invalid ASR transcript 同时不得上传当前 capture frames；
- valid transcript 仍走同一个 PersonRuntime。

## 8. Media / TTS regression

CI contract：

- WAV parsing/resampling；
- fake ASR/TTS；
- server separation；
- lazy dependency load；
- Windows native runtime safety contract；
- `tts_provider: sherpa` 走 `:8001` local path；
- `tts_provider: kokoro` 走 `:8001 -> :9002`；
- formal Browser endpoint 不依赖 Lab dropdown state；
- Kokoro voice config 支持 `zf_001..zf_004`；
- missing `:9002` 时 formal Kokoro path 返回明确服务错误，而不是悄悄伪装成功。

本地 benchmark：

```bash
uv run python scripts/benchmark_media.py --wav path/to/test.wav --iterations 20
```

测量 cold/warm ASR/TTS、HTTP total、RAM/VRAM，而不是凭感觉决定 GPU。

## 9. Settings regression

Settings Center 至少验证：

- `config.yaml` 只保存非敏感配置；
- `.env` 保存 allowlisted Secret；
- API 不回传 Secret 明文；
- legacy plaintext Secret 可迁移；
- migration backup 不复制 plaintext Secret；
- normal config save 创建 `.bak`；
- unknown/unrelated YAML key 和注释尽量保留；
- complete Settings validation 在写文件前发生；
- 保存后明确 `restart_required`，不制造局部 hot-reload 假象。

## 10. Sticker regression

至少验证：

- runtime catalog 合并 built-in + global + legacy manifests；
- `/v1/stickers` 的正式 scope 为 global；
- Web import 即使收到 legacy `character_id` 也写全局 user library；
- ZIP path traversal / archive size / uncompressed size / file count 有上限；
- manifest-last publication 不暴露半导入 pack；
- metadata 缺失时只有启用 AI tagger 才允许自动补标签；
- legacy character asset route 仍兼容已有客户端。

## 11. Smoke eval

`evals/smoke.jsonl` 保留作为最小 provider/runtime 冒烟数据。

`EvalRunner` 支持：

- 单/多 Character runtime；
- allowed actions；
- silence；
- message count；
- must/must-not contain；
- safe summary presence；
- min/max Memory write；
- minimum Recall count；
- Context contains。

## 12. Future evaluation work

后续候选：

- Model-as-Judge adapter；
- Persona Identification Accuracy；
- 10/50/100/500 turn persona decay；
- 30/100/365-day continuity；
- Forced Recall Rate；
- customer-service tone score；
- multi-action naturalness；
- cross-channel identity consistency（text/voice/image）；
- generated visual identity continuity。

Judge Model 必须与 Person Runtime 解耦，避免“换 Judge 等于改产品行为”。
