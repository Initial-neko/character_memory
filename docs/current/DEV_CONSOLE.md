# Dev Console

Character Memory Dev Console 是内部开发诊断 UI，默认运行在：

```text
http://127.0.0.1:8002/dev
```

它与正式 Chat UI、Settings Center、TTS Lab 分离，不拥有 Persona/Memory 数据，也不把云 Provider API Key 放到浏览器。

## 1. Recommended start

完整本地环境第一次准备：

```bash
bash scripts/setup-media-models.sh
```

日常启动：

```bash
uv run character-stack
```

当前 stack 会启动/检查：

```text
Character Runtime         :8000
Media Runtime             :8001
Dev Console               :8002
Settings Center           :8003
TTS Provider Runtime+Lab  :9002
```

可选 CosyVoice sidecar 使用 `:9012`，不属于主 stack 的强制依赖。

可选打开目标：

```bash
uv run character-stack --open chat
uv run character-stack --open settings
uv run character-stack --open tts
uv run character-stack --no-browser
```

`Ctrl+C` 只停止 launcher 自己启动的进程。已经提前独立运行的健康服务不会被 launcher 接管。

因此修改 Runtime 代码后，如果 Dev 页面仍表现得像旧代码，先检查是否复用了旧服务。

## 2. Independent diagnosis

需要单独启动服务时：

```bash
uv run character-memory web --port 8000
bash scripts/run-media.sh
uv run character-dev
uv run character-settings
uv run character-tts-lab
```

Dev deployment overrides：

```bash
export CHARACTER_DEV_CHARACTER_BASE_URL=http://127.0.0.1:8000
export CHARACTER_DEV_MEDIA_BASE_URL=http://127.0.0.1:8001
export CHARACTER_DEV_HOST=127.0.0.1
export CHARACTER_DEV_PORT=8002
export CHARACTER_CONFIG_PATH=config.yaml
```

## 3. Current cards

### System

检查 Character / Media Runtime health，并尽可能避免仅为了 health probe 提前加载完整本地模型。

Settings/TTS Lab 有自己的 health/status surface，不需要把所有配置管理职责复制到 Dev Console。

### 原始 payload 的展示约定

卡片在默认状态下只显示人读结论：badge、provider/device/latency、识别文本、模型回复。原始 JSON / 原始响应体一律放进折叠的 `<details class="debug-output">`，点开才出现——页面加载、卡片刷新和按钮点击都不会把 JSON 直接铺在页面上。执行类卡片（Vision / ImageGen / ASR / Media Smoke）在 `<details>` 之外留一行结论，失败时错误文本也写在这行，不藏在折叠块里。

折叠样式定义在 `ui.css`（不是 `dev.css`）：Chat 页的「本轮详情 / Runtime」抽屉用同一套折叠块，而 `index.html` 不加载 `dev.css`。

### 控件分层（levels）

Dev Console 的控件是手写 markup，没有 Settings Center 那样的 schema，所以分层契约直接写在 DOM 上，和 Settings 的 `common / advanced / diagnostic` 是同一套三级：

```html
<div class="level-row" data-level="common"> … 首屏控件 … </div>
<details class="level-group" data-level="advanced" data-title="高级 · …" data-hint="…">
  <summary>高级 · …</summary>
  … 折叠内容 …
</details>
```

判定规则只有一条：**一个控件属于离它最近的、声明了 `data-level` 的祖先**；`common` 直接渲染在卡片里，`advanced` / `diagnostic` 渲染成默认收起的 `<details class="level-group">`。

**没有声明 level 的控件落在 `diagnostic`**——和 Settings 的 `field_level()` 一样，漏标只会让控件被藏起来，不会跑到首屏。`web/dev_levels.js` 提供这条规则（`levelOf`）并做两件事：

- `sweep`：把任何没有声明祖先的控件移进所属卡片的 `diagnostic` 组（没有就建一个）。这是"漏标 = 不暴露"的运行时一半。
- `refreshSummaries`：用组内真实控件的 label 重写 `<summary>`，写成 `高级 · 调度与上限（6 项）：A · B · C … — hint`，所以收起的组一定说得出里面是什么，也不会和内容脱节。

`data-title` 是组的名字，`data-hint` 说明为什么需要打开它，两者都由 `tests/test_dev_console_levels.py` 强制。

当前首屏是 11 个控件（`common`），全部在 1440×900 的第一屏内：刷新状态、LLM 的 Prompt + Run LLM、Space / Group 各自的开关 + 目标选择 + 应用测试配置 + 立即手动触发一次。`first_screen` 的名单同样由测试固定：把一个控件提上首屏必须同时改 markup 和测试。

### LLM

通过服务端配置的 OpenAI-compatible Provider 发送开发 probe。

浏览器不传 API Key，也不持久化 key。

### Character Space Autonomy

用于验证真实的角色自主社交闭环，不是 mock。

支持：

- 选择一个未归档 Character；
- 临时热调整 Autonomous Space / Opportunity Interval / Max Posts per Day / Space Media / Max Media / Image Search / ImageGen / World Observation / World Pages / World Text / Audience / Scheduler Poll；
- 快捷档 `10min / 30min / 1H / 6H / 24H`；
- `立即手动触发一次`：立即跑一次完整 Space Opportunity，不改变正式 next time；
- `让选中角色立即到期`：把 next opportunity 设为现在，用真实后台 Scheduler 验证；
- 指定 Post ID 后 `再次模拟 Audience`；
- 强制执行一条 `SEARCH_IMAGE` 或 `GENERATE_IMAGE` 测试动态，不移动正式 Scheduler；
- 测试 1..9 张媒体上限、Search Query、SELFIE/SCENE Visual Intent；
- 测试 World Observation：Search Provider 发现 URL 后，用 Playwright 无头 Chromium 真正打开并执行 JS，返回抽取后的 WorldObservation；
- 无头浏览器打开 URL：只验证一个公开 URL 的渲染/正文抽取，不触发角色记忆或 Space 发帖；
- 查看每个人的 last/next opportunity、last status，以及最近 opportunity run history。

这里的 Space / Group Autonomy 调参只热应用到当前 Character Runtime，不写 `config.yaml`；正式值由 Settings Center 保存，重启 Character Runtime 后回到正式值。测试时可临时设为 1H 后让 stack 连续运行过夜，第二天直接从状态/动态/运行历史检查效果。

### World Activity

World Activity 的正式参数只在 Settings Center 保存。Dev Console 的整张 World Activity 卡位于 `diagnostic` 层，只用于真实手动验收：

```text
GET  /v1/dev/world/activity
GET  /v1/dev/world/pulse
POST /v1/dev/world/pulse/refresh
POST /v1/dev/world/pulse/{topic_id}/discuss
POST /v1/dev/world/browse/{character_id}
POST /v1/dev/world/activity/run
```

这里不会修改 `world_*` 配置。Pulse refresh 读取正式聚合页；discuss 让少量角色独立判断是否有话想说；Personal Browse 只形成近期 `WORLD_OBSERVATION(channel=PERSONAL_BROWSE)`，不会自动发 Space、Direct message 或直接写长期 Memory。
### Random Encounter

Random Encounter 的正式配置只在 Settings Center 保存。Dev Console 只提供状态观察与手动验收，并且整张卡位于 `diagnostic` 层，不进入首屏：

```text
GET  /v1/dev/encounters/status
POST /v1/dev/encounters/opportunity?source_type=AUTO|WEB|GENERATED
POST /v1/dev/encounters/due
```

`AUTO` 按正式的 `encounter_web_probability` 决定走互联网资料还是系统生成；`WEB` / `GENERATED` 用于强制验证单一路径。手动 opportunity 不修改正式 next opportunity；`due` 才把真实 Scheduler 的下一次机会设为现在。候选人物在用户明确留下前不占正式角色位。

### LLM Usage

LLM Usage Explorer 是 `diagnostic` 层能力，不进入 Dev Console 首屏。它统计真实 OpenAI-compatible `/chat/completions` HTTP 请求，而不是解析日志；正式运行态和 Dev probe 共用 `llm_calls` 计量表。

每条真实请求记录 Feature / Purpose、Model / Provider、Character / Conversation / Session、Logical Call ID / Attempt、latency、status、request id，以及 Provider 真正返回的 input/output/total token。Provider 不返回 `usage` 时，Token 保持未知，只保留字符数和 Token Coverage；不会用字符数伪造 token。

Structured Output repair/retry 属于同一个 Logical Call，但每个真实 HTTP attempt 单独计量，因此可以同时看到逻辑调用数、真实请求数和 Retry Rate。

Dev 页面可查看 1H / 24H / 7D / 30D：

- Requests / Logical Calls；
- Input / Output / Total Tokens 和 Token Coverage；
- Retry / Error Rate 与平均 latency；
- Feature + Purpose 聚合；
- Model 聚合；
- 最近真实请求。

Usage 只保存调用元数据和用量，不复制 Prompt / Response 正文；具体上下文继续由 Runtime Trace 承担，并通过 `llm_logical_call_id` 关联。

计量写入发生在用户等待回复的路径上，因此它让路于聊天：`llm_calls` 被别的连接锁住时，这一次记录在一百毫秒的预算内放弃（不排队、不重试），并记一条带堆栈的 warning；Explorer 因此可能少算个别请求，但绝不拖慢回复，也不会无声地少算。

```text
GET /v1/dev/llm-usage?hours=24&limit=80
GET :8000/v1/llm/usage?hours=24&limit=80
```

当前归因覆盖 Direct、Group、Space、Proactive、World、Persona、Ensemble、Encounter、Life、Avatar、Visual、Sticker 与 Dev probe。新增 LLM 能力时应在调用边界补 Feature / Purpose，而不是长期落到 `OTHER`。
### TTS / Media Smoke

Dev Console 不再提供第二个独立的 TTS 试听卡片。横向试听 Provider、Voice、Speed 和 A/B 对比统一使用 `:9002/tts` 的 TTS Workbench。

Dev Console 保留 **Media Live Smoke**：输入一段文本后，真实执行：

```text
正式 :8001/v1/tts
  -> WAV
  -> :8001/v1/asr
```

它使用 Settings Center 当前正式的 TTS Provider / Voice / Speed，不再暴露一套重复的 Speaker / Speed 控件。这样 Dev Console 负责“正式链路是否通”，Workbench 负责“Provider 横向试听/诊断”。

### ASR

支持：

- 上传 WAV；
- browser microphone recording；
- 转换成当前 Media ASR contract；
- 显示识别文本和 timing。

正式 Voice UI 还会在发送前执行 ASR transcript validity gate；Dev ASR 卡片主要用于观察原始识别结果与延迟。

### ASR Capture Review

采集回看：测试态打开后，**Media Runtime**（不是浏览器）把每次上传的 WAV 与它产生的文本逐条存下来，面板按时间倒序列出，每条可直接播放并对照识别结果。

关键约束：

- 存的是 Media Runtime 实际收到的那段音频，也就是模型的输入，不是重编码副本；
- 因此记录来自真实通话/听写路径，浏览器侧不存在第二条测试用采集链路；
- **默认关闭**，且关闭时不写入任何文件；开关只存在于 Media Runtime 进程内存中，重启即回到关闭；
- 「清空」是独立意图，不会因为关闭录制而被连带触发；
- 目录默认 `data/asr-capture`（`CHARACTER_MEDIA_ASR_CAPTURE_DIR` 可覆盖），按条数与总字节数自裁剪，且已被 `.gitignore` 排除——它是关于使用者本人的运行时数据。

用途是让 ASR 的改动可判定：音频里有而文本里没有 = 解码丢失；音频里就没有 = 采集丢失。两者需要相反的修法，没有这份配对数据就只能靠猜。

### Media Live Smoke

执行真实：

```text
TTS -> generated WAV -> ASR
```

用于验证当前本地 model/runtime 安装，不是假 provider test。

### ImageGen

当前已经是正式 Dev surface，不再是“未来规划”。

它调用 Character Runtime 的真实 visual APIs，支持：

- Character
- Provider (`agnes` / `msimg`)
- Purpose (`AVATAR / SELFIE / SCENE`)
- natural-language instruction
- use avatar reference
- Rewrite Prompt
- Generate Image
- polished prompt preview
- provider/model/duration
- Media ID
- actual generated image preview
- generated candidate -> current avatar

Dev ImageGen 可以持久化测试 MediaAsset，方便继续做 avatar/media 检查。

正式聊天页的显式 AI 生图默认先返回 data URL draft，只有用户最终点击发送时才进入聊天事实；Character 自主 ImageGen 则由 PersonReaction 决定并走异步 visual worker。

### Resource Monitor

按需显示：

- system RAM
- process RSS
- NVIDIA VRAM（可用时）

默认不要用高频 polling 把诊断工具变成资源来源本身。

### Metrics

显示 Media Runtime bounded latency buffer，帮助分辨 ASR/TTS warm path 和 HTTP total。

### 归档人物与语音

归档是生命周期标记，不是删除：`persona.yaml`、`voice.yaml` 和历史媒体都保留，但角色会立刻退出 active voice / GSV / proactive wake 路径。

- `/v1/voice-templates` 不再把 archived 角色放进正式语音快照；
- 对 archived 角色设置 voice 返回 409；
- GSV registry 扫描跳过 archived persona，并拒绝继续按 archived character id 合成；
- archive / restore 会 best-effort 触发当前 GSV registry reload；
- `voice.yaml` 不改写，因此 restore 后原 voice mapping 可以直接恢复。

archive / restore 响应中的 `voice_registry` 报告这次 reload 结果，`status` 区分三种：`reloaded`（sidecar 收下了新名单）、`rejected`（sidecar 用 reload route 自己定义的 400 拒绝——它确实在运行，也确实还持有旧名单）、`unreachable`（没有任何人给出拒绝：没有 sidecar、超时、或端口前面站着别的东西。回环上没人监听的端口不一定拒绝连接，TUN 模式的代理会回一个 502，所以"收到了 HTTP 响应"不算证据）。三种情况下归档本身都成功。

只有 `rejected` 会留下页级提示条，并且带一个可点的关闭按钮——它说"运行中的 GSV sidecar 可能仍按旧名单合成语音"，这话只有在 sidecar 答过话时才成立。`unreachable` 只给一条会自动消失的短提示，不声称有 sidecar 在运行：大多数部署根本没有 GSV sidecar（`dev_stack` 只在 `.external/GSV-TTS-Lite/.venv` 存在时启动它）。提示条容器保持 `pointer-events:none`，只有关闭按钮例外，因此它不会挡住底下的控件（此前它挡住侧边栏导致浏览器冒烟测点不中元素），也不会为了展示错误而阻止归档抽屉关闭。

## 4. Settings boundary

Dev Console 顶栏提供直接进入 `TTS Workbench :9002` 的入口，TTS Provider 试听与 VoiceDesign 工具仍由 Workbench 自己负责。

Dev Console 不是 Secret/config editor。

正式配置入口：

```text
http://127.0.0.1:8003/settings
```

持久化规则：

```text
config.yaml   non-sensitive config
.env          API keys / tokens
```

Settings Center 负责 legacy secret migration、config backup 和 restart-required policy。Dev Console 不复制这套逻辑。

### 4.1 页面新鲜度与脚本加载失败

Dev Console 的 HTML 与 `/static/*` 都返回 `cache-control: no-cache` + ETag，浏览器每次使用前都必须回服务器确认，所以普通 F5 拿到的就是当前配置，不需要强制刷新。这一层没有 Service Worker，也没有代理缓存。

真正的失败模式是脚本没有加载成功（例如页面正好在 stack 重启的窗口里加载，`/static/dev.js` 打到了已经停掉的端口）。这种页面仍然渲染，但显示的是 markup 里的占位值——例如 Interval 停在 `1440` 而不是运行中的值——并且所有控件都不响应。它不会自愈，必须重新加载。为了让这种情况不再伪装成"页面正常"：

- `dev.js` 初始化完成后在 `<body>` 上设置 `data-dev-booted="1"`；
- `dev.html` 的内联脚本在 3 秒后检查该标记，缺失就显示红色横幅「页面脚本没有加载成功」；
- Space 状态区显示 `读取时间 HH:MM:SS`，一眼能看出这份数据是什么时候读的；
- 页面从 bfcache 恢复（`pageshow` 且 `persisted`）时重新拉取 Space 状态与角色列表，避免恢复出 stack 重启前的旧配置。

## 5. World Browser diagnostics

World Browser belongs to Character Runtime, not to Dev Console itself. Dev only proxies these formal diagnostics:

    GET  /v1/dev/world/status
    POST /v1/dev/world/search
    POST /v1/dev/world/fetch

The search path first asks the configured SearchAPI/Brave provider for candidate URLs, then opens up to N public pages with Playwright headless Chromium. The fetch path opens one public URL to diagnose JavaScript rendering and readable-text extraction.

This is not a generic Postman surface: there are no custom headers, cookies or secrets, and local/private-network targets are rejected. Page text is untrusted external data; only the appraisal-safe summary may reach PersonRuntime Memory or final Space expression context.

For a managed local Chromium installation:

    uv run playwright install chromium

CI browser-smoke opens a JavaScript delayed-render fixture with real Chromium and asserts that the extracted body is the rendered text rather than the initial HTML.

## 6. Image provider diagnostics

`GET /v1/visual/providers` 返回：

- configured
- available
- reason
- supports_reference_images
- model

注意：`msimg configured=true, available=false` 通常表示当前 Character Runtime Python 环境没有可用 `msimg` dependency/runtime；它不是一次远程 ModelScope generation health probe。

完整 dev 环境应使用：

```bash
bash scripts/sync-all.sh
```

模型/语音资产缺失则运行：

```bash
bash scripts/setup-media-models.sh
```

## 7. Boundary

Dev Console 不是 generic Postman。

它应该只暴露 Character Memory 自身的诊断能力：

- runtime health
- LLM
- local media
- formal TTS
- visual generation
- World Observation 的受限公开 URL 渲染/搜索诊断
- resource/latency diagnostics

World Browser 的 URL 输入是这一条正式能力的诊断入口，不允许自定义 header/cookie/secret，也不能访问 localhost/私网。除此之外不要加入通用任意请求工具来绕过服务端边界。

## 8. Testing expectation

需要区分：

### Contract/CI

可以证明：

- route 存在；
- proxy shape 正确；
- fake/provider boundary 正确；
- Browser smoke 能加载页面并完成预定交互。

### Local live

才能证明：

- Windows native Media Runtime 真正可加载；
- SenseVoice/Sherpa/Kokoro 真实模型可推理；
- 正式 `:8001 -> :9002` Kokoro 路由可用；
- Agnes/ModelScope key 和网络真实可用；
- ImageGen 实际出图；
- 本机资源/延迟达到可接受水平。

不要把 CI green 表述成“本地真实 Provider 已验证”。
