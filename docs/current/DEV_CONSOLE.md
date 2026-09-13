# Dev Console

Character Memory Dev Console 是内部开发诊断 UI，默认运行在：

```text
http://127.0.0.1:8002/dev
```

它与正式 Chat UI 分离，不拥有 Persona/Memory 数据，也不把云 Provider API Key 放到浏览器。

## 1. Recommended start

完整开发环境：

```bash
bash scripts/sync-all.sh
uv run character-stack
```

启动/检查：

- Character Runtime `:8000`
- Media Runtime `:8001`
- Dev Console `:8002`

Dev Console 是本地开发前门。

可选：

```bash
uv run character-stack --open chat
uv run character-stack --no-browser
```

`Ctrl+C` 只停止 launcher 自己启动的进程。已经提前独立运行的健康服务不会被 launcher 接管。

因此修改 Character Runtime 后，如果 Dev 页面仍表现得像旧代码，先检查是否复用了旧 `:8000`。

## 2. Independent diagnosis

```bash
uv run character-memory web --port 8000
bash scripts/run-media.sh
uv run character-dev
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

检查 Character / Media Runtime health，并尽可能避免仅为了 health probe 提前加载完整 PersonRuntime。

### LLM

通过服务端配置的 OpenAI-compatible Provider 发送开发 probe。

浏览器不传 API Key，也不持久化 key。

### TTS

调用 Media Runtime TTS：

- 文本输入；
- WAV 播放；
- provider/device；
- inference/audio/RTF/total timing。

### ASR

支持：

- 上传 WAV；
- browser microphone recording；
- 转换成当前 Media ASR contract；
- 显示识别文本和 timing。

### Media Live Smoke

执行真实：

```text
TTS -> WAV -> ASR
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

Dev ImageGen 可以持久化测试 MediaAsset，方便继续做 avatar / media 检查。

正式聊天页的显式 AI 生图则默认先返回 data URL draft，只有用户最终点击发送时才进入聊天事实。

### Resource Monitor

按需显示：

- system RAM
- process RSS
- NVIDIA VRAM（可用时）

默认不要用高频 polling 把诊断工具变成资源来源本身。

### Metrics

显示 Media Runtime bounded latency buffer，帮助分辨 ASR/TTS warm path 和 HTTP total。

## 4. Image provider diagnostics

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

## 5. Boundary

Dev Console 不是 generic Postman。

它应该只暴露 Character Memory 自身的能力：

- runtime health
- LLM
- local media
- visual generation
- resource/latency diagnostics

不要加入任意 URL、任意 header、任意 secret 编辑器来绕过服务端边界。

Browser 主要和 Dev Console origin 交互；跨服务路由与 Provider credentials 留在 server-side。

## 6. Testing expectation

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
- ASR/TTS 真实模型可推理；
- Agnes/ModelScope key 和网络真实可用；
- ImageGen 实际出图；
- 本机资源/延迟达到可接受水平。

不要把 CI green 表述成“本地真实 Provider 已验证”。
