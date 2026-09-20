# GSV 音色模板设计（Design Spec）

- 日期：2026-09-20
- 修订：2026-09-20 —— 采纳「固化即建模板，角色只引用」，取消原形态一（§3.3）
- 基线：`main` @ `1b5c649`
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

本设计进一步发现**第三个分裂**：即使有了 `VoiceProfile`，角色侧仍然是「自带参考音频」，于是「一个角色的声音」和「一个可复用的声音」还是两件事。**本设计把「一个音色」收敛成唯一载体：模板。角色的 `voice.yaml` 退化成一行引用。**

---

## 2. 目标 / 非目标

### 目标

1. **模板**：一个具名 `VoiceProfile`，全局存放，用名字引用；这是仓库里**唯一**的声音载体。
2. **固化即建模板**：合成页的「固化到角色」写一个模板 + 让该角色引用它，不再产生角色私有的参考音频。
3. **角色只引用**：`personas/<id>/voice.yaml` 只剩一种形态 `template: <名>`。
4. **角色侧可选声线**：聊天页 drawer 增加声线面板，与既有头像管理并列（§11）。
5. 设置页 GSV 区块从 **5 个字段减到 3 个**。
6. **无缝续上**：legacy env → 模板；已有的每角色参考音频 → 模板 + 引用（§8）。
7. 共享底模：两个 MODEL 字段保留，所有模板复用。

### 非目标

- 不做模板的版本管理 / 历史。
- 不做模板的权限或共享。
- 不改 `tts_voice`（跨 provider 字段，见 §6.3）。
- 不自动清理 `.env`（见 §8）。
- **不做独立的「角色设置页」。** 每角色配置的既有落点是聊天页 drawer（头像管理、群名修改都在那里），声线面板并入它，而不是新开一个页面与它分家。

---

## 3. 概念与数据模型

**模板就是一个有名字的 `VoiceProfile`**，是唯一的声音载体。

### 3.1 角色的 `voice.yaml` —— 只有一种形态

```yaml
template: haru
```

就这一行。角色不持有参考音频，只持有**对模板的引用**。

`gpt_model` / `sovits_model` **不允许**出现在角色文件里。底模继承只发生在模板层（`null` = 继承全局 `GSV_TTS_GPT_MODEL`），否则「同一份音频在 A 角色下用底模 X、在 B 角色下用底模 Y」会造出一个无法解释的中间态。

### 3.2 模板文档

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

与既有 `VoiceProfile`（`voices.py:28`）**同 schema**，不新增字段。`voices.py:44-56` 的 docstring 已说明 `created_at`/`instruct`/`model` 为何必须**声明**而不是 `extra="ignore"`：拼错 `sovits_mdoel` 必须响亮报错，不能静默继承全局底模。

### 3.3 为什么取消「自带参考」形态

原设计有形态一（`ref_audio`/`ref_text` 直接写在角色文件里）与形态二（引用模板）并存，并规定二者互斥。**取消形态一**，因为：

- 两形态互斥本身就是要在 UI 里解释一个分支，而形态一能做的事形态二都能做——建一个同名的模板而已；
- 「角色专属」这个语义靠**命名约定**就能保留：固化默认用角色 id 作模板名，于是 `voices/haru.yaml` 就是「haru 的声音」；
- 共享仍然可能：角色 B 在 drawer 里指向 `haru` 模板即可（§11）。

**代价（明确接受）：**

| 代价 | 应对 |
|---|---|
| 角色专属音频会出现在全局模板列表里 | 命名约定让它们可辨认；drawer 里能看出「谁在用」 |
| 重复固化会**打断其它引用同一模板的角色** | §7.1 覆盖前提示「另有 N 个角色在用这个声音」 |
| 删模板会打断引用它的角色 | §5.2 要求显式报出，**绝不静默换声** |
| `personas/<id>/voice/` 目录不再产生 | 已有内容由迁移处理（§8） |

---

## 4. 存储布局

```text
voices/<模板名>.yaml                    # 模板定义
voices/<模板名>/<sha256[:16]>.wav       # 音频（内容寻址）
personas/<id>/voice.yaml                # 只有 template: <名>
```

与 `personas/` 平级。**`voices/` 整体 gitignore**，一条规则。

理由：模板里的 `ref_audio` 是**本机路径**（可能是仓库外的模型资产目录），本身不可移植；WAV 是本地资产。这与已忽略的 `config.yaml` / `.env` 同类。

> **顺带修掉一个潜在问题。** `.gitignore:25` 的规则是 `personas/character-*/`，只盖住自动生成的角色目录。**具名角色（`personas/haru/`）不被忽略**，所以原设计下 `personas/haru/voice/*.wav` 会被提交进仓库。新设计把全部音频归到 `voices/`（已忽略），而 `personas/<id>/voice.yaml` 是**配置**、应该提交——两者各归其位。

> **为什么模板不放进 `personas/`**：`discover_character_profiles()` 精确 glob `personas/*/persona.yaml`，模板放进去虽不会立刻被误认为角色，但把「角色」和「音色资产」两种生命周期不同的东西混在一棵树里，会让将来的目录级操作（清理、备份、导出角色）都得先分辨类型。

---

## 5. 解析顺序

GSV sidecar 收到 `voice: V` 时：

1. `V` 命中**已注册角色**（`personas/*/voice.yaml`）→ 读其 `template` → 用该模板
2. 否则 `V` **直接命中模板名**（`voices/<V>.yaml`）→ 用它
3. 否则 → **默认模板**（`GSV_TTS_VOICE`），并在 `X-TTS-Voice` / `GsvTtsResult.voice` 里回报**实际使用**的名字

模板名取自角色 id 时，第 1、2 级结果相同——这是命名约定的自然结果，不是特例。

**仍然降级不报错。** 浏览器给每个角色都发 `voice: <角色id>`，拒绝未知名字会让所有还没配声音的角色一起哑掉。这是 PR #56 已确立的契约，本次不改。（但「配了却失效」是另一回事，见 §5.2。）

`template: <名>` 的引用在**加载期**解析成具体的 `ref_audio`/`ref_text`，运行期不做二次查找——避免运行期出现「模板被删了」这种中途状态。

### 5.1 落点在 `_resolve_voice`

`_resolve_voice`（`gsv_tts_experiment.py:205-237`）返回 `(voice, ref_audio, ref_text, gpt_model, sovits_model)` 五元组。现在的三级是「注册表 → 未命中则 `self.ref_audio`/`self.ref_text`」。改后第一、二级都是查表（第 1 级多一跳角色 → 模板），**第三级换成默认模板的 ref**。

`self.ref_audio`/`self.ref_text` 的位置随之变化：设置页不再驱动它们（§6.1.1），只剩两个用途——迁移读取（§8），以及第三级查不到默认模板时的最后兜底。**不要删掉这两个字段**：删了会让 §13.3 那条「legacy 已被删」的路径变成无路可走。

第三级仍需**不抛异常**：理由与现状相同——`_resolve_voice` 处在每个请求的必经之路上，这里抛异常会让所有角色一起哑掉。

### 5.2 模板失效 ≠ 未配置（**两条路必须分开**）

| 情形 | 行为 |
|---|---|
| 角色**没有** `voice.yaml` | 静默用默认模板。这是「还没配声音」，降级是对的 |
| 角色**有** `voice.yaml`，但 `template:` 指向的模板不存在 / `ref_audio` 不可达 | **响亮失败**：`VoiceProfileError`，`status()` 里可见，drawer 里红字报出 |

第二行的理由：用户明确配过这个角色的声音，静默换成默认模板会让他以为还是那个声音——**错误的声音比没有声音更难发现**。这与 §5 的「降级」不矛盾：降级是为了保护**没配**的角色，不是为了掩盖**配坏**的角色。

这条也决定了 §5.1 第三级「不抛异常」的边界：只有**角色文件缺失**才走第三级；**角色文件存在但失效**在加载期就抛，不进入运行期。

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

## 7. 声音合成页（TTS Lab）变更

### 7.1 固化到角色 = 建模板 + 写引用

**输入**：`character_id`（走 §7.3 的白名单校验）+ 生成 artifact（`{text, language, instruct}` 快照）。

**三个写入，顺序不可换：**

```text
1. voices/<角色id>/<sha256[:16]>.wav      # 音频，内容寻址 → 幂等，重复写无害
2. voices/<角色id>.yaml                   # 模板，引用上面的 WAV
3. personas/<id>/voice.yaml               # template: <角色id>
```

这个顺序保证**崩溃只会留下孤儿，不会留下悬空引用**：WAV 写完崩 → 没人引用它；模板写完崩 → 角色仍指向旧模板，行为不变；第 3 步写完才生效。反过来写会造出「角色指向一个没有音频的模板」。

**模板名固定为角色 id，不可改。** 于是：
- 重复固化 = 覆盖 `voices/<角色id>.yaml`（已确认的决策）；
- 覆盖前**必须提示**：扫 `personas/*/voice.yaml` 里 `template == <角色id>` 的角色数，减去自己，若 > 0 则显示「另有 N 个角色正在使用这个声音，覆盖会一起改变它们」；
- 想要自由命名 / 无角色地建模板，走 §7.2 的「保存为模板」，两侧不重复。

> 「固化」与「保存为模板」的分工：前者是**给某个角色定声音**（不需要起名），后者是**攒一个可复用的声音**（需要起名）。两者产物同 schema，落点同在 `voices/`。

### 7.2 保存为模板

新增按钮，与「固化到角色」并列，**复用同一套 UI 守卫**（存有 artifact、且 text / instruct / language 仍与生成时快照一致时才可点）。

差异：

| | 固化到角色 | 保存为模板 |
|---|---|---|
| 前置条件 | 必须已有一个角色 | **不需要角色** |
| 模板名 | 固定 = 角色 id | **用户输入**（§7.3 校验） |
| 落点 | `voices/<角色id>.yaml` + `voice.yaml` 引用 | 只有 `voices/<名>.yaml`，不动任何角色 |
| 重名 | 覆盖（提示引用者数量） | **拒绝**（用户起的名字撞车是误操作，不是有意覆盖） |

### 7.3 模板名校验（只对 §7.2 的自由命名路径有效）

**固化路径不需要这套规则**：它的名字来自 `character_id`，而 `character_id` 的安全性来自**白名单**——必须能在 `discover_character_profiles()`（`config.py:172`）的结果里查到，查不到就拒绝。白名单比任何字符串规则都可靠。

自由命名没有列表可查，所以必须是一组**写死的字符串规则**：

- 非空（strip 后）；
- 拒绝路径分隔符（`/`、`\`）与 `..`；
- 拒绝绝对路径（含盘符）；
- 拒绝 Windows 保留名（`CON`、`PRN`、`AUX`、`NUL`、`COM1-9`、`LPT1-9`）与结尾的 `.`／空格——名字会变成文件名，这些在 Windows 上会让写入以难懂的方式失败；
- 建议限一个长度上限。

名字进的是 `voices/<名>.yaml` 与 `voices/<名>/` 两级路径，所以上面每一条都直接对应一个真实的写入失败或越界。

模板名与已有角色 id 同名时**允许**（解析顺序已定义，§5），但若与**已有模板**同名则拒绝并提示。

> 注意 `character_id = data.get("id") or path.parent.name`（`config.py:193`）——**目录名不是权威**，校验必须走 `discover_character_profiles()` 而不是自己拼目录名；写入目录从返回字典里的 `persona_path`（`config.py:204`）推导，**绝不把客户端传的 id 直接拼进路径**。

---

## 8. 迁移

两条独立的迁移，都在**首次加载时**触发，都只做一次，都**不修改 `.env`**（legacy 键保留在原处成为不再被读取的死配置，由用户自行删除——迁移只在文件系统写新文件，可逆且无副作用）。

**迁移 A：legacy env → 模板**

触发：`voices/` 为空 且 `GSV_TTS_REF_AUDIO` / `GSV_TTS_REF_TEXT` 两个 legacy 字段齐全。

动作：落成 `voices/<GSV_TTS_VOICE 或 "default">.yaml`，其中 `gpt_model: null` / `sovits_model: null`（继承 §6.1 保留的两个字段）。

**迁移 B：已有每角色参考音频 → 模板 + 引用**（新增，因 §3.3 取消形态一）

触发：存在 `personas/*/voice.yaml` 且其内容含 `ref_audio`/`ref_text`。

动作：对每个这样的角色，用其文件内容建 `voices/<角色id>.yaml`，并把角色文件改写成 `template: <角色id>`。

**WAV 复制不删**：`personas/<id>/voice/<sha>.wav` 复制到 `voices/<角色id>/<sha>.wav`，原处保留。沿用「迁移可逆、不碰用户的东西」原则；删原件留给用户决定。

**迁移顺序**：B 必须在 A 之前判定。两者可能同时满足（既有角色音频、又有 legacy env），此时产出两个独立模板，互不覆盖。

> **实现顺序是硬约束：迁移必须在严格校验之前跑。** 迁移 B 要读的正是**旧格式**的角色文件（含 `ref_audio`/`ref_text`），而 §10 规定角色文件里出现这些键要被 `VoiceProfileError` 拒掉（`extra="forbid"`）。于是「先用新 reader 校验、失败再迁移」这条路会把自己的输入判死，症状是升级后**所有已配置声音的角色一起报错**。正确顺序是：发现 `voices/` 为空 → 扫描并迁移 → 再交给严格 reader。这也是「迁移只在首次加载触发」这条规则的真正用途。

迁移后的提示信息里明确列出「这两个 legacy 键现在已不再生效，可以删除」。

---

## 9. 热更新

`POST /v1/voices/reload` 扩展为**同时重读模板与角色引用**，沿用同一端点而不是新增第二个——两棵树最终汇入同一个注册表，分开 reload 只会造出「模板新了但角色还旧」的中间状态。这与 `reload_voices()`（`gsv_tts_experiment.py:187-196`）的既有语义一致：纯文件重读，不碰引擎、不动显存，新参考音频在下次 `infer_batched` 时惰性缓存。

**固化只做文件 I/O + 一次 reload**，GSV 引擎不动、模型不重载，**下一个聊天回合就生效**（GSV 的两个缓存都是按路径索引、miss 时自填的普通 dict：`TTS.py:104-105` 定义，`:665-666` 与 `:685-688` 自填）。

设置页保存默认模板名时走既有 `configure` 路径——`GsvRuntimeConfigRequest.voice`（`:87`）已经映射到 `default_voice`（`:453`），**不需要新字段**。

> `GsvRuntimeConfigRequest` 是 `extra="forbid"`，且 `:75-79` 的注释明确说明为什么不能再加一个 `voices` 字段（`configure` 一检测到 tracked 字段变化就卸载引擎，纯文件变更不该丢热权重）。这个约束本身不变；模板清单走 `reload`，默认模板名走 `voice`。

### 显存时序

| 情形 | 结果 |
|---|---|
| 固化 | **不碰显存**，纯文件 I/O + reload |
| GSV 没运行 | 固化仍成功落盘，`activated:false` + 原因；GSV 下次启动即可用 |
| **生成时 GSV 已加载** | 这里才撞显存：`:9015` 懒加载 → CUDA OOM → **503**，什么都不写 |
| 生成时勾选「先卸载 GSV」 | 可选 opt-in：Workbench 先 POST `:9014/v1/unload` 再生成。**绝不自动**——静默卸掉 GSV 会打断进行中的对话 |

面板加被动提示：读 `/v1/providers/gsv`，若 `loaded:true` 就警告「GSV 已加载，显存可能不足；请先在 Settings Center 卸载，或勾选『生成前卸载 GSV』」。

---

## 10. 错误处理

| 情形 | 行为 |
|---|---|
| 角色 `voice.yaml` 的 `template:` 指向不存在的模板 | 加载期 `VoiceProfileError`（**响亮失败**，§5.2） |
| 模板的 `ref_audio` 不存在 | 同上（与既有 `ref_audio not found` 一致） |
| 角色文件里混入 `ref_audio`/`gpt_model` 等 | `VoiceProfileError`（`extra="forbid"` 已保证，但要写进角色侧的 readers） |
| 请求的音色名未注册 | 降级到默认模板（既有契约，§5） |
| 默认模板本身不可解析 | `ready: false` + 明确 reason；**stack 仍正常启动** |
| §7.2 保存模板时重名 | 拒绝并提示 |
| 固化时模板名 = 角色 id 已存在 | **覆盖**，但先提示引用者数量（§7.1） |

---

## 11. 角色侧：drawer 的声线面板（**新增**）

**落点**：聊天页 drawer，与头像管理并列。既有范式是 feature 模块（`avatars.js`、`group_settings.js`）：

```js
(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before voices.js");
  async function open(characterId = CM.state.characterId) {
    CM.openDrawer(`${profile.name || profile.id} · 声线`, "...");
    // 渲染进 CM.dom.drawerBody
  }
  CM.registerFeature("voice", {open});
})();
```

新建 `src/character_memory/web/voices.js`，`index.html` 加载，样式进**新建的 `voices.css`**——不要塞进既有的 `voice.css`：后者是**语音通话**（麦克风、VAD、播放），与**声线配置**名字相近但职能无关，合并会让两个不相关的改动互相冲突。

**面板内容**：

| 区域 | 内容 |
|---|---|
| 当前声线 | 读 `personas/<id>/voice.yaml`：显示模板名 + 该模板的来源（合成页冻结 / 手建）+ `created_at`/`instruct`（若有） |
| 未配置 | 提示「当前使用默认声线 `<GSV_TTS_VOICE>`」，并给「去合成页造一个」的入口 |
| **模板失效** | 红字报出（§5.2），给出「重新选择」与「打开 TTS Lab」两个动作 |
| 选择器 | 下拉列出全部模板，**来源复用既有的 `GET /v1/providers/gsv` 的 `voices`**（§6.2 已说明 `_voice_ids()` 会把模板并进去，不新增端点），选中即写 `voice.yaml: template: <名>` + 触发 reload |
| 谁在用 | 每个模板标注被几个角色引用，避免 §7.1 的覆盖打断变成意外 |

**写路径**：新增一个写 `personas/<id>/voice.yaml` 的端点（沿用 §7 的原子写：`.yaml.tmp` → `replace`，参照 `persona_builder.py:95-104` 的 `save_persona()`），写完调 `/v1/voices/reload`。**不要**让前端直接拼路径——同 §7.3，`character_id` 必须先过白名单。

**与设置页的分工**：设置页管**全局**（底模、默认模板、Provider）；drawer 管**这一个角色**。两者不重叠。

---

## 12. 测试策略

沿用 PR #56 的教训：**跨模块边界必须有往返测试**。

| 层 | 覆盖 |
|---|---|
| `voices.py` | 模板发现 / 角色引用解析 / 引用不存在时抛 `VoiceProfileError` / 角色文件混入 `ref_audio` 被拒 / §7.3 名字校验**每一条都要有反例** |
| `gsv_tts_experiment.py` | 三级解析顺序 / 未知名字降级到默认模板 / **配了却失效 → 抛，不降级（§5.2）** / reload 同时刷新两棵树 / `_asset_status()` 新就绪条件 |
| 迁移 | A：`voices/` 空 + legacy 齐全 → 建模板；B：已有角色音频 → 建模板 + 改写引用；两者同时满足 → 互不覆盖；`voices/` 非空 → 跳过；legacy 已被删 → 不报错（§13.3） |
| `tts_lab.py` | 固化三个写入的**顺序**（注入写失败，断言不出现悬空引用）/ §7.2 端点 / 名字校验 / 重名拒绝 / 覆盖时引用者计数 |
| `settings_server.py` | ① 删字段后 `_gsv_payload` **不发出** `ref_audio`/`ref_text` 键（§6.1.1，防抹空）② GSV 段拿到注入的模板 options，且模板为空时该字段 disabled |
| **往返** | 合成页写出的模板，能被 `voices.py` **读回来**（写者 ↔ 读者，参照 `test_frozen_voice_yaml_is_loadable_by_the_registry`） |
| drawer 端点 | 写 `voice.yaml` 后 reload 被调用；非法 `character_id` 被拒（路径安全） |
| 真机 | 端到端跑一次真实 `:9002 → :9014`，确认模板真的出声；并用**当前这份 `.env`** 验迁移 |

`_gsv_payload` 那条要写成**行为断言**（断言 payload 里没有这个键），不是断言源码字符串——本仓库有源码字符串匹配的测试（见旧 plan 风险 4），它们改不动就发现不了真问题。

---

## 13. 风险与开放问题

1. **迁移成了可用性的单点。** 就绪条件从「4 个字段齐全」变成「默认模板可解析」（§6.1），而把老配置变成模板的正是迁移 A（§8）。**迁移一旦不触发，GSV 从「能用」直接变「不可选」**——不是降级，是 Provider 从 Settings Center 消失。所以迁移不能只在单测里过，必须在真机上用**当前这份 `.env`** 验一遍：迁移前后 `status()["ready"]` 都是 true。这是本次最该先写测试、也最该真机验的一条。

2. **`_gsv_payload` 的空串陷阱（§6.1.1）会伪装成「更坏」而不是「报错」。** 忘了省略键 → 运行期 ref 被抹空 → 症状是「删完设置字段后 GSV 说不清哪里不对」，而不是一个明确的失败。这类静默失败本仓库已经吃过一次（见 `sidecar-schema-drift-fails-silent`），所以要求写成行为断言。

3. **迁移的正确性依赖 legacy 字段仍在。** 若用户在迁移前手动删了 `.env` 里的键，就没有可迁移的内容——此时应按「无模板」处理并提示去合成页创建，而不是报错。

4. **「固化即建模板」让覆盖成了默认行为，这是本设计最锋利的一处。** 若角色 B 引用了 `voices/haru.yaml`，而 haru 重新固化，B 的声音会**静默改变**。§7.1 要求覆盖前提示引用者数量，但那只是一个提示——用户仍可能点过去。若将来这成为真实困扰，退路是给模板加「被引用时禁止覆盖」或引入「另存」（本设计已明确不做的那个选项）。

5. **`voices/` 整体 ignore 意味着模板不进版本控制。** 换机器需要重建。这是本设计的直接后果，已确认接受。

6. **模板名与角色 id 的命名空间重叠。** 解析顺序定义了行为（§5），且固化路径默认让两者相同。但用户仍可能困惑于「`voices/haru.yaml` 和 `personas/haru/` 是什么关系」。§11 的面板要能把它讲清楚。

7. **`GSV_TTS_VOICE` 的语义变更**（音色名 → 模板名）是向后兼容的：迁移后模板名与原音色名相同，行为不变。但若用户曾把 `GSV_TTS_VOICE` 设成一个**从未被注册过**的名字，迁移会以该名建模板，之后解析结果才会一致。

8. **`tts_voice` 与默认模板可能指向不同名字**，而 gsv 路径上真正生效的是后者（§6.3）。这不是 bug（两者作用域不同），但若将来有人为了「改默认音色」去改 `tts_voice` 而没效果，会是一条难查的反馈。文档里已写明。

9. **`float_audio_to_wav` 在仓库里有三份重复**（`qwen3_tts_experiment.py:62`、`gsv_tts_experiment.py:60`、`media_runtime.py:110`）。本次不合并，但新代码应复用而非写第四份。
