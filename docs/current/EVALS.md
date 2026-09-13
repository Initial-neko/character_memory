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
- `GENERATE_IMAGE` 只在允许上下文出现；
- 无歧义字段 alias 可以归一化，但主 action 语义错误不能静默吞掉。

### Recall Precision

检查：

- 该想起的内容是否进入 Top-K；
- 不相关 Memory 是否侵入 Context；
- 是否发生 false recall；
- future barrier 是否有效。

### Memory Admission Precision

候选最后是否：

- `WRITE`
- `SKIP_LOW_VALUE`
- `SKIP_DUPLICATE`

普通寒暄不应持续污染长期 Memory；重要事件应有合理机会写入。

辅助 candidate 的 harmless schema drift 不应让有效主回复失败。

### Memory Provenance

派生 Memory 是否能回溯 source Event。群聊则要确认 shared fact 不会被错误复制成多个原始事实。

### Relationship / Re-encounter

共同经历和真实时间间隔是否影响后续行为，而不是只会事实问答。

Context 提供：

- 上次聊天时间；
- 距离上次聊天多久。

Eval 要禁止固定 `gap > N -> 好久不见` 模板。

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
- search/mentions/history；
- Sticker/Image/media contracts；
- ImageGen provider/runtime/API contracts；
- Dev Console proxy contracts；
- Media Runtime fake-provider/native bootstrap contracts。

### Browser smoke

CI 独立 browser job 安装 Playwright/Chromium，验证正式页面的关键交互 contract。

Browser extra 不属于默认 `all` dev sync，避免完整本地环境无条件安装浏览器依赖。

### Local live provider tests

CI green **不等于**以下真实链路已经验证：

- Windows sherpa native DLL；
- SenseVoice/VITS real model inference；
- Agnes API key / real image generation；
- ModelScope/msimg runtime；
- 本机 GPU/CPU 性能；
- 麦克风权限/真实录音。

这些必须在本机 Dev Console / benchmark 单独验收。

## 4. Async conversation regression

必须持续防止这些回归：

- 用户 Event 因 Provider 失败而消失；
- 更新的用户消息到了，旧 reaction 仍提交；
- fresh SSE stream 错过 `idle` 后永久显示“正在输入”；
- 一个 group member malformed output 把后续成员一起吞掉；
- group first reply 必须等待全部成员结束才显示；
- mention 被误解释成“只有被点名者有权限回应”。

## 5. Visual generation regression

至少区分：

### Character autonomous

- `SELFIE` / `SCENE` contract；
- non-USER_MESSAGE 不允许自主生成；
- SCENE 不强制 avatar reference；
- slow generation 不阻塞主文本；
- visual failure 不让主 reaction 失败；
- newer user event 可使旧生成结果 stale。

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

## 6. Media regression

CI contract：

- WAV parsing/resampling；
- fake ASR/TTS；
- server separation；
- lazy dependency load；
- Windows native runtime safety contract。

本地 benchmark：

```bash
uv run python scripts/benchmark_media.py --wav path/to/test.wav --iterations 20
```

测量 warm ASR/TTS、HTTP total、VRAM，而不是凭感觉决定 GPU。

## 7. Smoke eval

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

## 8. Future evaluation work

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
