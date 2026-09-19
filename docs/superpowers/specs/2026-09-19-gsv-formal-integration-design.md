# GSV-TTS 正式接入设计（Design Spec）

- 日期：2026-09-19
- 基线：`main` @ `7f22f8a`（"Add GSV-TTS-Lite Lab sidecar"）
- 状态：待评审 → 评审通过后转 implementation plan
- 相关文档：`docs/current/GSV_TTS_EXPERIMENT.md`（现状）、`docs/research/GSV_TTS_INTEGRATION.md`（早期提案，部分行号已过期）

---

## 1. 背景

`7f22f8a` 已经把 GSV-TTS-Lite 作为 **Lab-only provider** 接进来：sidecar 监听 `127.0.0.1:9014`，TTS Lab(`:9002`) 能试听。它**刻意没有**接入正式的 `:8001/v1/tts`。

本设计要解决的是"从 Lab 试听 → 正式聊天可用的角色语音"这一段。核心矛盾是：

> 当前 sidecar 是**单参考、启动时固定**的（`GSV_TTS_REF_AUDIO` / `GSV_TTS_REF_TEXT` 从 env 读一次，`voices` 只有 `["murasame"]`）。
> 而产品要的是**每个角色一个音色**。

同时存在一个已实测的**质量稳定性问题**（见 §3.3），它必须在正式接入前解决，否则接入的是一个不可复现的语音源。

---

## 2. 目标 / 非目标

### 目标

1. 每个角色拥有独立音色，采用**共享底模 + 每角色参考音频**方案（用户已确认）。
2. Murasame 保留其微调模型（`Murasame-e15.ckpt` + `Murasame_e8_s192.pth`），不因统一架构而降级。
3. 参考素材可通过 Qwen3-TTS 合成并**固化为资产**，在 TTS Lab 里完成"生成 → 试听 → 固化"。
4. 合成结果**可复现**：同一输入 + 同一 seed 得到同一音频。
5. GSV 作为正式 provider 接入 `:8001/v1/tts`，并消除现有的静默降级缺陷。

### 非目标

- 不做流式 / SSE / WebAudio 增量播放。V1 沿用完整 WAV（短句 0.6s 级，现有单槽预取够用）。
- 不做语音克隆的模型训练。微调流程不在本设计范围。
- 不改浏览器播放格式处理（`voice.js` 用 `response.blob()`，32kHz WAV 原生可播）。
- 不重构 Sherpa / Kokoro / Qwen3 / Edge 的既有行为。

---

## 3. 已验证的现状事实

以下均为本次在 `main` @ `7f22f8a` 上**实测**得到，不是推断。

### 3.1 环境与启动

- `torch 2.9.1+cu128`，CUDA available，RTX 5060 Laptop。
- sidecar 启动加载耗时 `load_ms=34638`，CUDA allocated `1445.3 MB`，`sample_rate=32000`。
- `GET :9014/health` → `ready=true, loaded=true, device=cuda, voices=["murasame"]`。
- TTS Lab `:9002` 中 gsv 条目 `ready=true, loaded=true`。
- `uv run pytest tests/test_gsv_tts_experiment.py tests/test_tts_lab.py tests/test_media_runtime.py tests/test_settings_center.py` → **32 passed**。

模型资产位置（注意：**不在本仓库**）：

```
C:/Users/cute/projects/Shinsekai/data/models/deepseek1/
  Murasame-e15.ckpt      (155 MB)
  Murasame_e8_s192.pth   (135 MB)
  MAY_0035.wav           (164 KB)
```

共享底模已缓存在 `~/.cache/gsv/`：`s1v3.ckpt`、`s2Gv2ProPlus.pth`。

### 3.2 延迟

| 场景 | inference |
|---|---|
| 首次请求（preload=1，仍冷） | **4131 ms** (RTF 1.17) |
| 短句稳态 | 477–910 ms |
| 28 字句稳态 | 930–1658 ms |

`probe_latency_tuned.json` 记录短句 mean 0.61s，与本次稳态实测吻合。

> **要点**：`preload=1` 加载了模型但**没有预热 reference 编码**，首句仍付 ~4s。任何"preload 已消除冷启动"的说法不成立，延迟预算必须包含这一项。

### 3.3 稳定性缺陷（阻塞项）

同一段文本连跑三次，音频时长：

```
6.00s (192127 frames) / 10.72s (343039 frames) / 6.68s (213759 frames)
```

偏差 ~78%。原因：`gsv_tts_experiment.py` 的 `GsvTtsRequest` 只有 `text/voice/language/speed`，**没有 seed 或采样参数**，透传到底层 `TTS.infer_batched` 的默认值 `temperature=1.0, top_k=15, top_p=1.0` —— 完全随机。

10.72s 那次（28 字句）高度疑似重复/拖长，这是用户主观反馈"有时候声音有些奇怪"的主要嫌疑来源。

### 3.4 正式链路的结构缺陷

`media_server.py` 的 `POST /v1/tts` 是三分支结构，**没有 `else`、没有未知 provider 报错**：

| 分支 | 行 | 行为 |
|---|---|---|
| qwen3 | `media_server.py:174-205` | → `{qwen3_base}/v1/tts` |
| kokoro \| edge | `media_server.py:207-238` | → `{tts_lab_base}/v1/tts`，body 带 `provider` |
| fallthrough | `media_server.py:240-264` | 进程内 Sherpa |

`selected="gsv"` 今天会**穿透两个 if，静默返回 Sherpa 语音，且不产生任何日志**。

`/health` 同样撒谎：`media_server.py:105` 的 `if selected not in {"kokoro", "edge"}` 会让 `selected="gsv"` 复用本地 Sherpa runtime 的 `ready`/`loaded`，只把 `provider` 字段改名为 `"gsv"`。

### 3.5 每角色音色当前不存在

- `personas/*/persona.yaml` 的键固定为 `id/name/identity/tagline/description/personality/behavior/boundaries`，**无任何 voice/tts/speaker 字段**。
- `config.py:34` 的 `tts_voice` 是**全局单值**，一个进程一个。
- 唯一的"每角色"机制是 `web/voice.js:84-91` 的 3 桶哈希 `TTS_SPEAKER_IDS=[0,2,5]`，**只对 Sherpa 生效**；且 `voice.js:591` 只发 `speaker_id`，从不发 `voice`，所以 kokoro/qwen3/edge 下所有角色声音完全相同。

---

## 4. 设计

### A. 音色层：参考注册表（voice registry）

sidecar 从"单参考"升级为"参考注册表"：

```
voice_id → {
  ref_audio: path,
  ref_text: str,
  gpt_model?: path,      # 可选 override
  sovits_model?: path,   # 可选 override
}
```

- **默认**：共享底模 `s1v3.ckpt` + `s2Gv2ProPlus.pth`，仅参考音频不同 → 满足"共享底模 + 每角色参考"。
- **override**：Murasame 条目指向其微调模型 → 保留现有质量，不降级。
- **缓存**：底模只加载一份（VRAM 一份）；每个 voice 的 reference embedding 做 LRU 缓存。首次使用某 voice 付一次编码成本，之后命中缓存。
- **preload 列表**：可配置启动时预热哪些 voice，避免首句 4s 冷启动（§3.2）。

`GsvTtsRequest.voice` 保持现有字段名，语义从"固定标签"变为"注册表键"，对 Lab 与正式链路都是向后兼容的扩展。

> **为什么不是每角色一个 sidecar 进程**：每个进程一份模型 ≈1.4GB VRAM。N 个角色线性吃显存，不可接受。

### B. 参考素材：Qwen3 合成 → 固化为资产

在 TTS Lab 增加流程：**选 Qwen3 音色 + 输入参考文本 → 合成 → 试听 → 满意则固化**。

固化产物落在角色目录：

```
personas/<name>/voice/ref.wav
personas/<name>/voice/ref.txt
```

**必须固化，不能每次运行时生成**，理由：

1. 可复现、可审计 —— 同一个角色的音色不会因为某次 Qwen3 采样不同而漂移。
2. 不引入运行时对 Qwen3 sidecar 的依赖（正式链路只依赖 GSV）。
3. 参考文本与音频**天然完美对齐** —— 这是 zero-shot 克隆最难的一步（人工转写几乎必有错字），用合成反而白送。

**已知风险（Phase 0 必须先验证）**：GSV 在真人语音上训练，合成参考普遍缺少呼吸/唇齿/底噪细节，克隆结果可能发闷、带金属味、音色不稳。若 Phase 0 听感不达标，B 方案需改为人工录制或真人素材剪辑，A 的架构不受影响。

### C. 稳定性控制

- sidecar 请求模型增加 `seed`（int，可选）、`temperature`、`top_k`、`top_p`。
- 固定 `seed` 时同一输入必须产出同一音频（测试断言用）。
- 默认值从当前的 `temperature=1.0, top_k=15` 收紧到更保守的取值，降低跑飞概率。
- 增加**时长/重复保护**：对生成 token 数或音频时长设上限，超限截断并记日志，避免 10.72s 那类输出直接进正式链路。

### D. 正式链路接入

`media_server.py` 新增 gsv 分支与 health 分支，**同一次改动必须补上 unknown-provider 守卫**（§3.4）：未知 `tts_provider` 返回明确错误（4xx/5xx），不得静默降级到 Sherpa。并补回归测试。

需改动的硬编码注册点（共 10 处 + 打包/文档）：

| # | 位置 | 内容 | 漏掉的后果 |
|---|---|---|---|
| 1 | `config.py:33` | `pattern=r"^(kokoro\|sherpa\|qwen3\|edge)$"` | pydantic 校验失败，`"gsv"` 根本存不进去 |
| 2 | `dev_stack.py:71` | 白名单 `{"kokoro","sherpa","qwen3"}` | **静默变 kokoro，sidecar 不启动**（`edge` 已漏，证明此处易失守） |
| 3 | `dev_stack.py:209-238` | `if tts_provider == "qwen3":` 启动块 | GSV sidecar 不启动、无 health gate |
| 4 | `media_server.py:174` | qwen3 分支 | **不需要改**（GSV 不走这条路），仅需知道它存在 |
| 5 | `media_server.py:207` | kokoro/edge 分支 | 集合漏加 `gsv` → 穿透到 Sherpa |
| 6 | `media_server.py:240-249` | 裸 fallthrough | **静默降级根因** |
| 7 | `media_server.py:83` / `:105` | health 分发 | `/health` 谎报 gsv 就绪 |
| 8 | `settings_store.py:65-70` | provider 下拉选项 | Settings Center 选不到 gsv |
| 9 | `tts_lab.py:610-621` | Lab 注册表 | 已存在（`tts_lab.py:614`） |
| 10 | `settings_store.py:76-98` | 扁平 21 项 `tts_voice` 联合 | **不要**往里加 gsv，见下 |

**关于 #10**：`settings_store.py:76-98` 把所有 provider 的音色拍平成一个 21 项下拉，`tts_provider` 与 `tts_voice` 之间**零校验耦合**。每角色音色的正确归宿是 §3.5 指出的新机制（角色级），而不是继续扩充这个全局扁平列表。

**路由方式（已定，不要二选一）**：复用 Lab 路径。`media_server` 不新增第三个 `*_base` env，而是把 `gsv` 并入既有的 kokoro/edge 集合，走 `POST {tts_lab_base}/v1/tts` 带 `{"provider": "gsv"}`。

具体到表内行号：#4（`:174`）**不改**；#5（`:207`）把集合从 `{"kokoro", "edge"}` 改为 `{"kokoro", "edge", "gsv"}`；#7（`:105`）同样处理；#6（`:240`）加守卫。

选它的理由： kokoro/edge 那条路径已经处理好了「provider 透传 + `content-type` 回退 + `X-TTS-*` → `X-Media-*` 头映射」，GSV 的响应头与 Qwen3 同构，接进去几乎零新增逻辑。另起一个 `gsv_base` 会复制一整套同样的代码。

> 注意 `tts_lab.py:614` 读取的是 `CHARACTER_TTS_GSV_BASE`（默认 `:9014`），而 `scripts/start-gsv-tts.sh:33` 设置的是 `GSV_TTS_PORT`。**监听端口与客户端 URL 是两个独立变量、无一致性校验** —— 这与 Qwen3 的既有坑相同。改端口时必须同时改两处。

### E. 浏览器与设置

- **播放无需改动**：`voice.js:594` 用 `response.blob()`，不读 `Content-Type`，32kHz WAV 原生可播。
- **每角色音色**：`voice.js:591` 目前只发 `speaker_id`。改为把 `item.characterId`（`voice.js:586` 已在手）放进 `voice` 字段，服务端映射到注册表。
- **Settings Center schema 驱动**：加 provider 只需改 `settings_store.py`，不动 `settings.js`/`settings.html`。

---

## 5. 分阶段

| Phase | 内容 | 退出条件 |
|---|---|---|
| **0. Spike（决策门）** | Qwen3 合成一段参考 → 重启 GSV 指向它 → 与 `MAY_0035.wav` 参考 A/B 试听 | **听感是否可接受**。不达标则 B 改人工素材，架构不变 |
| 1. 稳定性 | seed / temperature / top_k 透传 + 时长保护 | 固定 seed 同输入产出同一音频；跑飞不再进链路 |
| 2. 音色层 | 参考注册表 + 按请求切参考 + LRU 缓存 + preload 列表 | 同一进程内两个 voice 交替请求均正确 |
| 3. 正式接入 | `media_server` gsv 分支 + health 分支 + **unknown-provider 守卫** + `dev_stack` 白名单 | `:8001/v1/tts` 在 `tts_provider: gsv` 下返回 GSV 音频；未知 provider 不再静默降级 |
| 4. Lab UI | 参考生成 / 试听 / 固化流程 + 角色目录落盘 | 不走命令行即可为一个角色产出并固化参考 |

Phase 0 成本低（起 Qwen3 sidecar + 重启一次 GSV），但决定 B 是否成立，**必须先做**。

Phase 1 必须在 Phase 3 之前：否则接入的是不可复现的语音源。

---

## 6. 测试策略

- **不要**依赖 `tests/test_settings_center.py:146-167`、`tests/test_dev_stack.py:4-27`、`tests/test_tts_lab.py:305-350` 那类**读源码断言字符串**的测试作为正确性证据。它们会在纯重构时误报。新增测试应以行为为准。
- 参照 `tests/test_media_runtime.py` 的注入模式：monkeypatch `media_server.load_settings` + 注入 fake `provider_http_client`。仿 `_QwenProviderClient` 造 `_GsvProviderClient`。
- **必须新增**：未知 `tts_provider` 的回归测试（**当前完全空缺**，是静默降级无人发现的原因）。
- 稳定性测试：固定 seed 跑两次，断言字节一致。
- 注册表测试：同进程两个 voice 交替请求，断言各自音色对应正确（用 fake runtime，不需要 CUDA）。

---

## 7. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 合成参考克隆质量差（§4.B） | B 方案不成立 | Phase 0 先验证；备选人工/真人素材 |
| 首句 ~4s 冷启动 | 聊天首句明显卡顿 | preload 列表预热常用 voice（Phase 2） |
| 静默降级未修复就接入 | 用户听到 Sherpa 却以为在听 GSV | Phase 3 强制同 commit 加守卫 + 回归测试 |
| `dev_stack.py:71` 白名单失守 | sidecar 不启动且无报错 | Phase 3 显式覆盖，并修正 `edge` 的既有遗漏 |
| 读源码式测试阻碍重构 | 非行为性失败 | 新测试一律行为导向（§6） |
| 参考素材许可 | 角色音色涉及第三方声优 | 固化资产需记录来源与授权状态（本设计暂不强制，列入开放问题） |

---

## 8. 验收标准

1. `tts_provider: gsv` 时，`:8001/v1/tts` 返回真实 GSV 音频（非 Sherpa），响应头 `X-Media-Provider: gsv`。
2. 未知 `tts_provider` 返回错误，**不再**静默降级。
3. 同一 `seed` + 同一输入，两次合成字节一致。
4. 同进程内 `murasame`（微调）与任一共享底模 voice 交替请求，各自音色正确且不互相污染。
5. 一个角色可仅通过 Lab UI 完成"Qwen3 生成参考 → 试听 → 固化 → 在正式链路使用"。
6. `/health` 对 gsv 的报告与实际路由一致，不再复用 Sherpa 状态。

---

## 9. 开放问题

1. `ref_text` 是否需要支持非中文/日文（多语言参考）？当前 `GSV_TTS_PROMPT_LANGUAGE` 默认 `auto`，未验证。
2. Murasame 之外的角色的 `persona.yaml` 是否要新增 `voice:` 块作为注册表来源，还是集中在独立的 voice 配置文件中？（涉及 §3.5 的存储位置决策，需在 Phase 2 前定）
3. 参考素材的版权/授权如何记录？
4. 时长保护的上限取值（按 token 还是按音频秒数）需要实测确定。
