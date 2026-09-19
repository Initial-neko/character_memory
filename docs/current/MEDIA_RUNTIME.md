# Media Runtime — Local ASR / Formal TTS

Media Runtime 负责本地语音模型与正式 Browser 语音路由，与 Character Runtime 分离。

核心目标不是建立第二个 Voice Agent，而是：

```text
speech
  -> local ASR
  -> same Character Runtime
  -> same Person / Memory / Mental State
  -> formal TTS
```

## 1. Architecture

```text
Browser microphone
  ↓ PCM16 WAV / VAD
Media Runtime :8001
  ↓ SenseVoice ASR
Character Runtime :8000
  ↓ normal async chat / same PersonRuntime
  ↓ SSE character events
Media Runtime :8001/v1/tts
  ├─ sherpa -> local VITS
  ├─ kokoro -> :9002/v1/tts -> Kokoro
  ├─ edge -> :9002/v1/tts -> Microsoft Edge online TTS
  └─ qwen3 -> :9013/v1/tts -> Qwen3-TTS
Browser playback
```

完整开发栈：

```text
Character Runtime         :8000
Media Runtime             :8001
Dev Console               :8002
Settings Center           :8003
TTS Provider Runtime+Lab  :9002
optional CosyVoice        :9012
```

推荐统一启动：

```bash
bash scripts/setup-media-models.sh
uv run character-stack
```

独立诊断时：

```bash
uv run character-memory web --port 8000
bash scripts/run-media.sh
uv run character-dev
uv run character-settings
uv run character-tts-lab
```

## 2. Ownership boundary

Character Runtime owns：

- Persona
- Event / Memory / Mental State
- cloud LLM / Vision
- conversation/SSE

Media Runtime owns：

- ASR model lifecycle
- Sherpa TTS model lifecycle
- audio parsing/resampling
- formal `/v1/tts` routing
- local media inference timings

TTS Provider Runtime `:9002` 当前 owns：

- Kokoro model lifecycle
- provider audition API/UI
- Sherpa proxy entry for lab comparison
- optional CosyVoice sidecar proxy

Media Runtime 不 import / instantiate `PersonRuntime`。停止 Media Runtime 不应让纯文本聊天不可用。

## 3. Dependencies

Canonical full-dev environment：

```bash
bash scripts/sync-all.sh
```

Canonical local model setup：

```bash
bash scripts/setup-media-models.sh
```

`setup-media-models.sh` 会先复用 `sync-all.sh`，然后准备：

- SenseVoice ASR；
- Sherpa VITS；
- Kokoro `v1.1-zh` model + voice packs。

`scripts/setup-tts-models.sh` 仍存在，但只是 compatibility wrapper 到 `setup-media-models.sh`；新文档不要再把它当主入口。

`pyproject.toml` 的 Sherpa native stack 当前锁定：

```text
sherpa-onnx==1.13.5
sherpa-onnx-core==1.13.5
```

Kokoro 依赖进入 canonical `all` extra；CosyVoice 仍保持独立 Python 3.10 sidecar 环境。

## 4. Models

### ASR

```text
models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/
```

关键文件：

- `model.int8.onnx`
- `tokens.txt`

### Sherpa TTS fallback

```text
models/sherpa-onnx-vits-zh-ll/
```

关键文件：

- `model.onnx`
- `tokens.txt`
- `lexicon.txt`
- `dict/`
- rule FSTs

### Kokoro formal default

当前模型：

```text
hexgrad/Kokoro-82M-v1.1-zh
```

预下载文件：

```text
config.json
kokoro-v1_1-zh.pth
voices/zf_001.pt
voices/zf_002.pt
voices/zf_003.pt
voices/zf_004.pt
```

当前有效中文 voice：

```text
zf_001
zf_002
zf_003
zf_004
```

旧 `zf_xiaobei / zf_xiaoni / zf_xiaoxiao / zf_xiaoyi` 不属于当前 `v1.1-zh` voice 文件。

预下载只消除 request-time 网络下载，不消除首次 PyTorch deserialize、Chinese G2P 初始化和 CPU warmup。

## 5. Formal TTS routing

Browser 始终调用：

```text
POST :8001/v1/tts
```

正式选择由 `config.yaml` / Settings Center 控制：

```yaml
tts_provider: "kokoro"     # kokoro | qwen3 | edge | sherpa
tts_voice: "zf_001"
tts_speed: 1.0
tts_device: "cpu"          # cpu | cuda for Kokoro
```

### Kokoro

```text
Browser
  -> :8001/v1/tts
  -> :9002/v1/tts {provider=kokoro}
  -> Kokoro
  -> WAV back through :8001
```

正式 Browser 不需要知道 provider-specific URL，也不需要增加新的跨 origin TTS contract。

### Edge TTS

当 `tts_provider: edge`，`:8001` 通过 `:9002` 调用 Edge TTS。它是在线 Provider，无需 API Key，但 synthesis 依赖公网。默认 voice 为 `zh-CN-XiaoxiaoNeural`。Edge 原生 MP3 会以 `audio/mpeg` 原样返回，Browser 当前 Blob/Audio 播放链可直接处理，不做额外 WAV 转码。

### Sherpa

当 `tts_provider: sherpa`，`:8001` 直接使用本地 Sherpa VITS。兼容 voice 为 `0 / 2 / 5`。

### Lab choice is not production config

`:9002/tts` 的 provider/voice 下拉只用于试听。切换 Lab 选项不会写入正式配置；正式默认值在 Settings Center 修改，并按 V1 restart policy 重启 stack 后生效。

## 6. ASR configuration

`dev_stack.py` 会为本地模型设置默认路径。独立运行时可使用环境变量覆盖，例如：

```bash
export CHARACTER_MEDIA_ASR_MODEL="$PWD/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx"
export CHARACTER_MEDIA_ASR_TOKENS="$PWD/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/tokens.txt"
export CHARACTER_MEDIA_ASR_DEVICE=cpu
export CHARACTER_MEDIA_ASR_THREADS=2
export CHARACTER_MEDIA_ASR_LANGUAGE=auto
```

ASR 默认语言保持 `auto`。只有明确 benchmark/识别质量证明固定语言更优时，再本地 override。

## 7. Windows native runtime safety

Windows 上最重要的约束不是“ONNX 能 import 就行”，而是必须加载**当前 venv 与 sherpa wheel 匹配的 native runtime**。

启动路径会：

1. 优先项目 `.venv/Scripts`；
2. 使用 `os.add_dll_directory` 加入合法 DLL 目录；
3. 尝试 preload venv 中匹配的 `onnxruntime.dll`；
4. 拒绝静默使用 `C:\Windows\System32\onnxruntime.dll` 作为 fallback。

不要通过替换 System32 DLL 解决项目依赖问题。

## 8. HTTP contracts

Media Runtime：

```text
GET  /health
POST /v1/asr
POST /v1/tts
GET  /v1/metrics/recent
```

TTS Provider Runtime + Lab：

```text
GET  /tts
GET  /health
GET  /v1/providers
POST /v1/tts
```

Character chat 仍通过普通 async conversation path 和 SSE。Voice 前端不绕过 PersonRuntime。

`/v1/asr` 是同步 FastAPI route，因为本地 SenseVoice inference 本身是阻塞调用。这样 FastAPI 会把 ASR inference 放入 worker threadpool，而不是占住 event loop；因此 TTS request 可以与 ASR request 重叠进行。

## 9. Voice interaction model — Pipeline V1.1

当前 Voice 仍然**不是 full-duplex Voice Agent**，也没有 barge-in；但 microphone capture 已与 TTS playback 状态解耦。

### 9.1 TTS pipeline

每一个 Character 的一次可朗读 MESSAGE 仍然是一个完整 TTS unit，不按句子拆碎：

```text
Character A complete MESSAGE
  -> synthesize A
  -> play A

while A is playing:
  -> pre-synthesize only next queued MESSAGE B

A playback ends
  -> play already synthesized B
  -> pre-synthesize C
```

约束：

- TTS inference concurrency = 1；
- audio playback concurrency = 1；
- 当前音频播放可以与下一条 TTS inference 重叠；
- 不允许多个 Character 声音重叠播放；
- 当前不做 sentence/chunk streaming TTS。

这消除了旧实现中 `synthesize A -> play A -> synthesize B -> play B` 的人物之间空档，同时保持完整 MESSAGE 的语气连续性。

### 9.2 ASR stays live during TTS

Browser microphone 使用：

```text
echoCancellation=true
noiseSuppression=true
autoGainControl=true
```

TTS playback 期间，VAD / microphone capture / ASR 仍可继续运行。

如果用户在 AI 正在说话时讲话：

```text
AI TTS continues playing
  +
user speech -> VAD -> ASR -> transcript validity gate
  -> pending user turn
```

**用户说话不会停止或打断当前 TTS。**

有效 transcript 在播放期间只进入 `pendingTurns`；等当前 TTS playback queue 完全清空后，才通过正常 chat API 提交给同一个 PersonRuntime。连续捕获到多个 pending utterance 时，当前 V1.1 会把文本按顺序合并为下一次 user turn，并把 transient Visual Capture frames 去重后最多保留 4 帧。

因此逻辑顺序保持：

```text
Character finishes current spoken turn
  ↓
pending user speech is submitted
  ↓
normal async chat / PersonRuntime
  ↓
next Character reaction
```

而不是在 Character 仍播放旧回答时启动下一次 PersonRuntime reaction。

### 9.3 Capture state vs UI state

Voice 前端维护两类状态：

```text
phase
  = UI / conversation state
  listening | waiting | speaking | error | ...

capturePhase
  = microphone VAD state
  idle | listening | recording | transcribing | paused
```

这样 `phase=speaking` 时仍可保持 `capturePhase=listening`，避免旧实现因为进入 `speaking` 就丢弃 microphone frames。

在 transcript 已正式提交、Character Runtime 正在处理下一轮时，capture 会暂时 `paused`；当前 V1.1 的目标是“AI 播放时仍听得到用户”，不是允许无限并发用户 turn。

### 9.4 Transcript validity gate

```text
empty / punctuation-only   -> reject
any Han character          -> accept
ASCII Latin/digit >= 2     -> accept
otherwise                  -> reject
```

无效 transcript 不创建 chat message，也不上传当前通话中的 Visual Capture frames。

## 10. Resource policy

- 主 LLM / Vision 保持 cloud-only。
- ASR/TTS 可 CPU 运行；GPU 是 benchmark 证明有收益后的 accelerator，不是默认假设。
- `tts_device: cuda` 只在当前 Torch build 真正暴露 CUDA 时有效。
- ASR 与 TTS 可以请求级重叠；不要把这扩张成无限并发 media inference。
- Browser Voice 自己把 TTS inference 串行为 1，并只预取下一条。
- ASR/TTS combined VRAM hard target 仍以轻量为目标；不为了“有 GPU”强制常驻所有模型。
- 模型 lazy load 是合理策略；不要因为 health probe 就强制加载全部本地模型。
- CI 不下载真实模型、不要求 GPU。

## 11. Benchmarks

真实本地 benchmark：

```bash
uv run python scripts/benchmark_media.py \
  --wav path/to/test.wav \
  --iterations 20
```

重点拆分：

- ASR HTTP total
- ASR inference
- TTS HTTP total
- TTS inference
- cold vs warm load
- process RAM / optional VRAM

Voice Pipeline V1.1 额外关注：

- A playback 剩余时间是否足以覆盖 B synthesis；
- A -> B 实际 playback gap；
- TTS playback 期间 ASR latency；
- speaker playback 被 microphone 回采后的 AEC 效果；
- ASR + Kokoro CPU overlap 时的 realtime factor / CPU saturation。

Kokoro/Sherpa/CosyVoice 的音质横向试听应在 `:9002/tts` 做；正式 Browser latency 则应通过 `:8001/v1/tts` 验证路由后的真实路径。

## 12. Test layers

### CI contract tests

不需要真实 model/GPU：

- WAV parsing/resampling
- fake ASR/TTS provider
- formal TTS routing contract
- HTTP contracts
- ASR worker-thread route boundary
- lazy dependency boundary
- bootstrap/native runtime safety
- main server / media server separation
- frontend Voice transcript gate
- TTS next-message prefetch / serialized inference contract
- playback-period pending ASR contract
- Settings/TTS configuration wiring

### Local real-model test

验证本机：

- cold/warm load
- inference latency
- Mandarin quality
- DLL/device
- CPU/GPU resource
- Kokoro formal routing
- speaker/microphone echo cancellation during real TTS playback
- no audible gap between prefetched group replies when synthesis finishes in time

### Dev Console / TTS Lab

`:8002/dev` 验证正式服务链路；`:9002/tts` 用于多 Provider audition/benchmark。两者用途不同。

## 13. Current non-goals

当前仍未把以下能力作为稳定 contract：

- WebRTC full duplex
- barge-in / user speech interrupting TTS
- streaming ASR partials
- streaming TTS chunks
- sentence-level TTS pipeline
- voice cloning
- emotion/prosody control
- group call transport
- character-initiated call
- persistent raw audio

如果后续进入这些能力，应继续保持“Media 是渠道，Person 只有一个”的边界。
