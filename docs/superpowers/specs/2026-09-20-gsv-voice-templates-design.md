# GSV 音色模板设计（Design Spec）

- 日期：2026-09-20
- 基线：`main` @ `a4e99c8`（"Merge pull request #56"，每角色音色注册表 + 固化流程已落地）
- 状态：待评审 → 评审通过后转 implementation plan
- 相关文档：`docs/current/GSV_TTS_EXPERIMENT.md`、`docs/current/QWEN3_VOICE_DESIGN_TOOL.md`

---

## 1. 背景

PR #56 打通了「设计音色 → 固化到角色 → 聊天里出声」。但**全局默认音色**仍是老样子：靠 5 个松散字段拼出来。

```text
GSV_TTS_GPT_MODEL / GSV_TTS_SOVITS_MODEL / GSV_TTS_REF_AUDIO / GSV_TTS_REF_TEXT / GSV_TTS_VOICE
```

于是同一个概念——「一个音色」——在仓库里有**两套表示**：

| | 表示 | 位置 |
|---|---|---|
| 每角色音色 | `VoiceProfile`（结构化，Pydantic 校验） | `personas/<id>/voice.yaml` |
| 全局默认音色 | 5 个 env 字段 | `.env` |

这个分裂有两个具体代价：

1. **`GSV_TTS_REF_TEXT` 要手打**，而它必须与参考音频逐字一致。手打正是零样本克隆最难的失败模式——固化流程已经用「用产生音频的那句话当转写」从构造上消除了它，但全局默认这条路还留着。
2. 两套表示会**独立演化**。PR #56 修的那个静默 bug 就是这类漂移：写者与读者各自拥有一份 schema，没人测往返。

---

## 2. 目标 / 非目标

### 目标

1. **模板**：一个具名 `VoiceProfile`，全局存放，用名字引用。
2. 设置页 GSV 区块从 **5 个字段减到 3 个**。
3. 模板可在**声音合成页**构建（生成 → 试听 → 保存为模板），**不需要先有角色**。
4. 现有配置**无缝续上**：迁移成同名模板，行为不变。
5. 共享底模：两个 MODEL 字段保留，所有模板复用。

### 非目标

- 不做模板的版本管理 / 历史。
- 不做模板的权限或共享。
- 不改 `tts_voice`（跨 provider 字段，见 §6.3）。
- 不自动清理 `.env`（见 §8）。

---

## 3. 概念与数据模型

**模板就是一个有名字的 `VoiceProfile`**，与角色 `voice.yaml` **同 schema、同加载器（`voices.py`）**。名字即 `voice_id`。

不引入第二种数据结构——这是本设计的核心约束。模板只是「存放位置不同 + 被全局引用」的同一个东西。

### 3.1 角色 `voice.yaml` 的两种形态（互斥）

```yaml
# 形态一：自带参考（固化流程产出，现状不变）
ref_audio: voice/ffa77b6a3acab112.wav
ref_text: 你好，今天天气不错，我们出去走走吧。

# 形态二：引用模板（本次新增，作为 fallback）
template: murasame
```

**同时出现 `template` 与 `ref_audio`/`ref_text` 是错误**，报 `VoiceProfileError`。静默取其一会让配置文件看起来生效而实际没有。

`gpt_model` / `sovits_model` 在两种形态下都可选覆盖，语义不变（`null` = 继承底模）。

### 3.2 模板文档

与 `voice.yaml` 相同的 schema 加上溯源字段：

```yaml
voice_id: murasame          # 可省略，默认取文件名
ref_audio: C:/Users/cute/projects/Shinsekai/data/models/deepseek1/MAY_0035.wav
ref_text: ねえねえ、何があったの？ 顔色、すごく悪いけど。
gpt_model: null             # null 继承全局底模（GSV_TTS_GPT_MODEL）
sovits_model: null
created_at: '2026-09-20T...' # 溯源，GSV 不读
instruct: 年轻女性声线…      # 仅冻结产物有
model: Qwen/Qwen3-TTS-...   # 仅冻结产物有
```

---

## 4. 存储布局

```text
voices/<模板名>.yaml                    # 模板定义
voices/<模板名>/<sha256[:16]>.wav       # 从合成页冻结而来的自包含音频
```

与 `personas/` 平级。**`voices/` 整体 gitignore**，一条规则。

理由：模板里的 `ref_audio` 在方案 A 下是**本机路径**（可能是仓库外的模型资产目录），本身不可移植；WAV 是本地资产。这与已忽略的 `config.yaml` / `.env` 同类。

> 为什么模板不放进 `personas/`：`discover_character_profiles()` 精确 glob `personas/*/persona.yaml`，模板放进去虽不会立刻被误认为角色，但把「角色」和「音色资产」两种生命周期不同的东西混在一棵树里，会让将来的目录级操作（清理、备份、导出角色）都得先分辨类型。

---

## 5. 解析顺序

GSV sidecar 收到 `voice: V` 时：

1. `V` 命中**已注册角色音色**（`personas/*/voice.yaml`）→ 用它
2. 否则命中**模板**（`voices/*.yaml`）→ 用它
3. 否则 → **默认模板**（`GSV_TTS_VOICE`），并在 `X-TTS-Voice` / `GsvTtsResult.voice` 里回报**实际使用**的名字

**角色音色优先于同名模板**（更具体者胜）。

**仍然降级不报错。** 浏览器给每个角色都发 `voice: <角色id>`，拒绝未知名字会让所有还没配声音的角色一起哑掉。这是 PR #56 已确立的契约，本次不改。

`template: <名>` 的引用在**加载期**解析成具体的 `ref_audio`/`ref_text`，运行期不做二次查找——避免运行期出现「模板被删了」这种中途状态。

### 5.1 落点在 `_resolve_voice`

`_resolve_voice`（`gsv_tts_experiment.py:205-237`）返回 `(voice, ref_audio, ref_text, gpt_model, sovits_model)` 五元组。现在的三级是「注册表 → 未命中则 `self.ref_audio`/`self.ref_text`」。改后第一、二级都是查表，**第三级换成默认模板的 ref**。

`self.ref_audio`/`self.ref_text` 的位置随之变化：设置页不再驱动它们（§6.1.1），只剩两个用途——迁移读取（§8），以及第三级查不到默认模板时的最后兜底。**不要删掉这两个字段**：删了会让 §12.3 那条「legacy 已被删」的路径变成无路可走。

第三级仍需**不抛异常**：默认模板缺失时退回运行期现有 ref（可能为空），让 `ready: false`（§6.1）去拦住正常路径。理由与现状相同——`_resolve_voice` 处在每个请求的必经之路上，而浏览器给**每个**角色都发 `voice: <角色id>`，这里抛异常会让所有还没配声音的角色一起哑掉。

---

## 6. 设置页变更

### 6.1 字段处置

| 字段 | 处置 |
|---|---|
| `GSV_TTS_GPT_MODEL` | **保留** — 共享底模 |
| `GSV_TTS_SOVITS_MODEL` | **保留** — 共享底模 |
| `GSV_TTS_REF_AUDIO` | **删除** — 由模板提供 |
| `GSV_TTS_REF_TEXT` | **删除** — 由模板提供 |
| `GSV_TTS_VOICE` | **语义变为「默认模板」**，改为下拉选择 |

就绪判断在 `_asset_status()`（`gsv_tts_experiment.py:259-274`），现在硬编码校验那 4 个 legacy 字段，且这个函数经 `status()["ready"]` 一路决定「Provider 是否可选」。改为校验**两个 MODEL + 默认模板可解析**（默认模板存在，且其 `ref_audio` 文件可达）。

### 6.1.1 删字段的连带后果：`_gsv_payload` 必须**省略**键，不能发空串

`settings_server.py:110-117` 的 `_gsv_payload` 把设置值映射成 `configure` 的请求体。删掉两个字段后 `merged.get("GSV_TTS_REF_AUDIO")` 变成 `""`，而 `configure` 对空串的处理是**有区别的**（`gsv_tts_experiment.py:459-473`）：

```python
if raw is None: continue        # None = 不修改
value = str(raw).strip()
if attr == "default_voice": value = value or DEFAULT_VOICE   # 只有 4 个属性有空值兜底
...
if getattr(self, attr) != value: setattr(self, attr, value)  # ref_audio/ref_text 不兜底
```

`ref_audio` / `ref_text` **不在**那 4 个兜底属性之列。所以照现有写法发 `""` 会**把运行期 ref 抹成空**——表现为删完设置字段后 GSV 反而更坏。

改法：`_gsv_payload` 对这两个键**不写入**（或写 `None`），而不是写 `""`。这一条必须有回归测试，因为它是「删了两个字段」这个动作唯一不明显的副作用。

### 6.2 下拉的动态选项（**既有模式，不是新机制**）

`settings_store.py` 里的字段确实是静态 schema（`type: "select"` + 写死的 `options` 列表），但**注入运行期选项的机制已经存在**：`settings_server.settings_snapshot()`（`:149-191`）deep-copy 静态 schema，按 **section id** 找到「voice」段，按 **field name** 找到 `tts_provider` / `tts_voice`，把从实时状态算出的列表写进 `field["options"]`。

它已经处理好了两种边界，直接照搬即可：

- **当前值不在列表里** → 插一条 `"(当前配置)"` 前缀的项（`:163-172`、`:181-189`），避免下拉把正在用的值显示成空；
- **Provider 未就绪** → 选项全部 `disabled`（`:158`、`:178`）。

所以 GSV 区块要做的只是**同一个模式再应用一次**：对 `id="gsv-runtime"` 段做同样的查找与注入。不需要新的字段类型，也不需要第二个真相来源。

两个下拉的**作用域不同**，不要共用一份列表：

| 字段 | 段 | 选项来源 | 范围 |
|---|---|---|---|
| `GSV_TTS_VOICE` | `gsv-runtime` | `voices/*.yaml` | **仅模板**——它按定义必须命名一个模板 |
| `tts_voice` | `voice`（既有） | `status()["voices"]`（既有，`settings_server.py:176`） | 该 provider 能解析的所有名字 |

`tts_voice` 那一路**不用改**：`gsv_tts_experiment.py:198-203` 的 `_voice_ids()` 本就返回「已注册 id + `default_voice`」，把模板并进去即可（§5 的解析顺序第三级），下拉会自动跟着变。

若模板列表为空，`GSV_TTS_VOICE` 渲染为**禁用 + 提示**「尚无模板，请到 TTS Lab 的声音合成页创建一个」，而不是一个空下拉让人无从下手。

### 6.3 `tts_voice` 不动

`config.yaml: tts_voice` 是**跨 provider** 字段（kokoro 的音色名、sherpa 的 speaker id、gsv 的音色名），本次不改。

在 gsv 路径上它的实际作用有限：浏览器总会发 `voice: <角色id>`，`media_server` 转发该值，sidecar 解析失败后落到**默认模板**——所以决定「没配声音的角色用什么」的是 `GSV_TTS_VOICE`（默认模板），不是 `tts_voice`。

这一点写进文档，避免将来有人以为改 `tts_voice` 能改默认音色。

---

## 7. 声音合成页变更

新增「**保存为模板**」按钮，与既有「固化到角色」并列，**复用同一套 UI 守卫**（存有 artifact、且 text / instruct / language 仍与生成时快照一致时才可点）。

差异：

| | 固化到角色 | 保存为模板 |
|---|---|---|
| 前置条件 | 必须已有一个角色 | **不需要角色** |
| 落点 | `personas/<id>/voice.yaml` + `voice/<sha>.wav` | `voices/<名>.yaml` + `voices/<名>/<sha>.wav` |
| 额外输入 | 选角色 | 输入模板名 |

### 7.1 模板名校验（**不能照搬 `character_id` 的做法**）

`character_id` 的安全性来自**白名单**：固化流程要求它能在 `discover_character_profiles()` 的结果里查到，查不到就拒绝。模板名是**新建的**，没有列表可查，所以这里必须是一组**写死的字符串规则**：

- 非空（strip 后）；
- 拒绝路径分隔符（`/`、`\`）与 `..`；
- 拒绝绝对路径（含盘符）；
- 拒绝 Windows 保留名（`CON`、`PRN`、`AUX`、`NUL`、`COM1-9`、`LPT1-9`）与结尾的 `.`／空格——名字会变成文件名，这些在 Windows 上会让写入以难懂的方式失败；
- 建议限一个长度上限。

名字进的是 `voices/<名>.yaml` 与 `voices/<名>/` 两级路径，所以上面每一条都直接对应一个真实的写入失败或越界。

模板名与已有角色 id 同名时**允许**（解析顺序已定义，§5），但若与**已有模板**同名则拒绝并提示，避免静默覆盖一个不可再生的音频引用。

---

## 8. 迁移

触发条件（首次加载时）：**`voices/` 为空** 且 `GSV_TTS_REF_AUDIO` / `GSV_TTS_REF_TEXT` 两个 legacy 字段齐全。

动作：落成 `voices/<GSV_TTS_VOICE 或 "default">.yaml`，其中 `gpt_model: null` / `sovits_model: null`（继承 §6.1 保留的两个字段）。

**不修改 `.env`。** legacy 键保留在原处（成为不再被读取的死配置），由用户自行删除。这是刻意的：迁移只在文件系统写一个新文件，不碰用户的配置文件，可逆且无副作用。迁移后的提示信息里会明确列出「这两个键现在已不再生效，可以删除」。

迁移**只做一次**（`voices/` 非空即跳过），不会覆盖用户后来创建的模板。

---

## 9. 热更新

`POST /v1/voices/reload` 扩展为**同时重读模板与角色音色**，沿用同一端点而不是新增第二个——两棵树最终汇入同一个注册表，分开 reload 只会造出「模板新了但角色还旧」的中间状态。这与 `reload_voices()`（`gsv_tts_experiment.py:187-196`）的既有语义一致：纯文件重读，不碰引擎、不动显存，新参考音频在下次 `infer_batched` 时惰性缓存。

设置页保存默认模板名时走既有 `configure` 路径——`GsvRuntimeConfigRequest.voice`（`:87`）已经映射到 `default_voice`（`:453`），**不需要新字段**。

> `GsvRuntimeConfigRequest` 是 `extra="forbid"`，且 `:75-79` 的注释明确说明为什么不能再加一个 `voices` 字段（`configure` 一检测到 tracked 字段变化就卸载引擎，纯文件变更不该丢热权重）。这个约束本身不变；模板清单走 `reload`，默认模板名走 `voice`。

---

## 10. 错误处理

| 情形 | 行为 |
|---|---|
| `template:` 指向不存在的模板 | 加载期 `VoiceProfileError`（**响亮失败**，与既有 `ref_audio not found` 一致） |
| 模板的 `ref_audio` 不存在 | 同上 |
| `template` 与 `ref_audio` 同时出现 | `VoiceProfileError` |
| 请求的音色名未注册 | 降级到默认模板（既有契约） |
| 默认模板本身不可解析 | `ready: false` + 明确 reason；**stack 仍正常启动**（Phase 0 已确立） |
| 保存模板时重名 | 拒绝并提示 |

---

## 11. 测试策略

沿用 PR #56 的教训：**跨模块边界必须有往返测试**。

| 层 | 覆盖 |
|---|---|
| `voices.py` | 模板发现 / `template:` 引用解析 / 与自带参考互斥 / 名字校验（§7.1 每一条都要有**反例**） |
| `gsv_tts_experiment.py` | 三级解析顺序 / 同名时角色优先 / 未知名字降级 / reload 同时刷新两棵树 / `_asset_status()` 新就绪条件 |
| 迁移 | `voices/` 为空 + legacy 齐全 → 生成模板；`voices/` 非空 → 不覆盖；legacy 已被删 → 不报错（§12.2） |
| `tts_lab.py` | 「保存为模板」端点 / 名字校验 / 重名拒绝 |
| `settings_server.py` | ① 删字段后 `_gsv_payload` **不发出** `ref_audio`/`ref_text` 键（§6.1.1，防抹空）② GSV 段拿到注入的模板 options，且模板为空时该字段 disabled |
| **往返** | 合成页保存的模板，能被 `voices.py` **读回来**（写者 ↔ 读者，参照 `test_frozen_voice_yaml_is_loadable_by_the_registry`） |
| 真机 | 端到端跑一次真实 `:9002 → :9014`，确认模板真的出声 |

`_gsv_payload` 那条要写成**行为断言**（断言 payload 里没有这个键），不是断言源码字符串——本仓库有源码字符串匹配的测试（见旧 plan 风险 4），它们改不动就发现不了真问题。

---

## 12. 风险与开放问题

1. **迁移成了可用性的单点。** 就绪条件从「4 个字段齐全」变成「默认模板可解析」（§6.1），而把老配置变成模板的正是迁移（§8）。**迁移一旦不触发，GSV 从「能用」直接变「不可选」**——不是降级，是 Provider 从 Settings Center 消失。所以迁移不能只在单测里过，必须在真机上用**当前这份 `.env`** 验一遍：迁移前后 `status()["ready"]` 都是 true。这是本次最该先写测试、也最该真机验的一条。

2. **`_gsv_payload` 的空串陷阱（§6.1.1）会伪装成「更坏」而不是「报错」。** 忘了省略键 → 运行期 ref 被抹空 → 症状是「删完设置字段后 GSV 说不清哪里不对」，而不是一个明确的失败。这类静默失败本仓库已经吃过一次（见 `sidecar-schema-drift-fails-silent`），所以要求写成行为断言。

3. **迁移的正确性依赖 legacy 字段仍在。** 若用户在迁移前手动删了 `.env` 里的键，就没有可迁移的内容——此时应按「无模板」处理并提示去合成页创建，而不是报错。

4. **模板名与角色 id 的命名空间重叠。** 解析顺序定义了行为（角色优先），但用户可能仍会困惑。合成页在保存时若发现同名的**角色**存在，应给出提示（不阻止）。

5. **`voices/` 整体 ignore 意味着模板不进版本控制。** 换机器需要重建。这是方案 A 的直接后果，已确认接受。

6. **`GSV_TTS_VOICE` 的语义变更**（音色名 → 模板名）是向后兼容的：迁移后模板名与原音色名相同，行为不变。但若用户曾把 `GSV_TTS_VOICE` 设成一个**从未被注册过**的名字，迁移会以该名建模板，之后解析结果才会一致。

7. **`tts_voice` 与默认模板可能指向不同名字**，而 gsv 路径上真正生效的是后者（§6.3）。这不是 bug（两者作用域不同），但若将来有人为了「改默认音色」去改 `tts_voice` 而没效果，会是一条难查的反馈。文档里已写明。
