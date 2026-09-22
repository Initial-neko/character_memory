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

### LLM

通过服务端配置的 OpenAI-compatible Provider 发送开发 probe。

浏览器不传 API Key，也不持久化 key。

### Character Space Autonomy

用于验证真实的角色自主社交闭环，不是 mock。

支持：

- 选择一个未归档 Character；
- 热调整 Autonomous Space / Opportunity Interval / Max Posts per Day / Space Media / Max Media / Image Search / ImageGen / World Observation / World Pages / World Text / Audience / Scheduler Poll；
- 快捷档 `10min / 30min / 1H / 6H / 24H`；
- `立即手动触发一次`：立即跑一次完整 Space Opportunity，不改变正式 next time；
- `让选中角色立即到期`：把 next opportunity 设为现在，用真实后台 Scheduler 验证；
- 指定 Post ID 后 `再次模拟 Audience`；
- 强制执行一条 `SEARCH_IMAGE` 或 `GENERATE_IMAGE` 测试动态，不移动正式 Scheduler；
- 测试 1..9 张媒体上限、Search Query、SELFIE/SCENE Visual Intent；
- 测试 World Observation：Search Provider 发现 URL 后，用 Playwright 无头 Chromium 真正打开并执行 JS，返回抽取后的 WorldObservation；
- 无头浏览器打开 URL：只验证一个公开 URL 的渲染/正文抽取，不触发角色记忆或 Space 发帖；
- 查看每个人的 last/next opportunity、last status，以及最近 opportunity run history。

这里的调参只热应用到当前 Character Runtime，不写 `config.yaml`；Group Autonomy 卡片同理。两者的正式值都由 Settings Center 保存，Dev 的测试值只活到这次 Character Runtime 进程结束：测试时可临时设为 1H 并让 stack 连续运行过夜，第二天直接从状态/动态/运行历史检查效果；重启后回到 Settings Center 的正式值。

### Random Encounter

Dev 只做观察和手动触发，正式调度参数不在这里改：

```text
Settings Center -> Random Encounter
  encounter_enabled / encounter_interval_minutes
  encounter_web_probability / encounter_max_pending
  encounter_poll_seconds
```

Dev 侧对应的诊断入口：

```text
GET  /v1/dev/encounters/status
POST /v1/dev/encounters/opportunity?source_type=AUTO|WEB|GENERATED
POST /v1/dev/encounters/due
```

`AUTO` 按 `encounter_web_probability` 决定走真实互联网资料来源还是系统生成，`WEB` / `GENERATED` 强制其中一条。手动触发不移动正式 next opportunity；`立即到期` 才把 next opportunity 设为现在，交给真实 Scheduler。候选角色在用户明确留下前不占正式角色位。

### ASR

支持：

- 上传 WAV；
- browser microphone recording；
- 转换成当前 Media ASR contract；
- 显示识别文本和 timing。

正式 Voice UI 还会在发送前执行 ASR transcript validity gate；Dev ASR 卡片主要用于观察原始识别结果与延迟。

### Media Live Smoke

执行真实：

```text
TTS -> generated WAV -> ASR
```

用于验证当前本地 model/runtime 安装，不是假 provider test。这里的 TTS 走正式 Media Runtime `POST :8001/v1/tts`，因此用的是 `config.yaml` 当前选中的 provider（Sherpa / Kokoro / GSV）及其到 `:9002` 的正式路由；请求体只带后端默认值，没有第二个 provider picker。横向试听请用 `:9002/tts` 的 TTS Lab。

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

归档是生命周期标记，不是删除：`persona.yaml`、`voice.yaml` 和历史媒体都留在磁盘上，但该角色立刻退出正式语音面：

- 不再进入 `/v1/voice-templates` 的正式语音快照与分配；
- 对它设置语音返回 409；
- 运行中的 GSV sidecar 拒绝为它合成，重载后不再解析这个 id。

`POST /v1/characters/{id}/archive` 与 `/restore` 的响应带一个 `voice_registry` 字段，报告这次 best-effort 重载的结果。voices 树里只要有一个模板不可解析，重载就整体回滚，该字段是 `{"ok": false, "reloaded": false, "reason": ...}`，而归档本身仍然返回 200——这时 sidecar 还在按旧名单合成。Character Archive 抽屉会把这条失败直接显示出来；修好模板后再归档/恢复一次，或重启 GSV sidecar。

restore 复活原来的映射：`voice.yaml` 从未被改写，重载成功后该 id 立即重新可用。

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
