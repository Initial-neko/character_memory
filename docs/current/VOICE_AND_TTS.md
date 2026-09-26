# Voice & TTS Runtime

本文统一描述 Character Memory 的语音基础设施与 TTS 工具链。所有语音能力都是同一个 Persistent Person 的输入/表达渠道，不建立第二套 Voice Agent。

文档 owner 边界：

- 本文件：ASR、formal TTS routing、Provider Runtime、Workbench、GSV、Qwen3 实验与 VoiceDesign 工具。
- `CONVERSATION_RUNTIME.md`：Direct / Group 中 durable Voice Message 的消息语义与持久化生命周期。
- `SETTINGS_CENTER.md`：正式 Provider / Voice / Secret 的持久化配置 ownership。
- `DEV_CONSOLE.md`：诊断和 smoke，不拥有正式配置。

原先五份 TTS/Media 专题文档已合并到本文件。

## Media Runtime — Local ASR / Formal TTS

Media Runtime 负责本地语音模型与正式 Browser 语音路由，与 Character Runtime 分离。

核心目标不是建立第二个 Voice Agent，而是：

```text
speech
  -> local ASR
  -> same Character Runtime
  -> same Person / Memory / Mental State
  -> formal TTS
```

### 1. Architecture

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
  └─ gsv -> :9002/v1/tts -> :9014 GSV-TTS-Lite
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
experimental Qwen3-TTS     :9013 (manual only; not formal chat)
optional GSV-TTS-Lite      :9014
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

### 2. Ownership boundary

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

TTS Provider Runtime / Workbench `:9002` 当前 owns：

- Kokoro model lifecycle；
- Kokoro / Edge / GSV formal provider adapters；
- multi-provider audition / benchmark UI；
- optional CosyVoice proxy；
- optional Qwen3 VoiceDesign Workbench tooling。

Sherpa 模型仍只由 Media Runtime 持有。Workbench 试听 Sherpa 时调用专用
`POST :8001/v1/providers/sherpa/tts`，不会经过正式 `/v1/tts` selector，也不会加载第二份 Sherpa。

Media Runtime 不 import / instantiate `PersonRuntime`。停止 Media Runtime 不应让纯文本聊天不可用。

### 3. Dependencies

Canonical full-dev environment：

```bash
bash scripts/sync-all.sh
```

Canonical local model setup：

```bash
bash scripts/setup-media-models.sh
```

`setup-media-models.sh` 会先复用 `sync-all.sh`，然后准备：

- local BGE Embedding cache；
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

### 4. Models

#### ASR

```text
models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/
```

关键文件：

- `model.int8.onnx`
- `tokens.txt`

#### Sherpa TTS fallback

```text
models/sherpa-onnx-vits-zh-ll/
```

关键文件：

- `model.onnx`
- `tokens.txt`
- `lexicon.txt`
- `dict/`
- rule FSTs

#### Kokoro formal default

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

### 5. Formal TTS routing

Browser 始终调用：

```text
POST :8001/v1/tts
```

正式选择由 `config.yaml` / Settings Center 控制：

```yaml
tts_provider: "kokoro"     # kokoro | edge | gsv | sherpa
tts_voice: "zf_001"        # provider-specific; Settings only offers voices reported by the healthy provider
tts_speed: 1.0
tts_device: "cpu"          # local provider device where supported; Edge ignores it
```

#### Kokoro

```text
Browser
  -> :8001/v1/tts
  -> :9002/v1/tts {provider=kokoro}
  -> Kokoro
  -> WAV back through :8001
```

正式 Browser 不需要知道 provider-specific URL，也不需要增加新的跨 origin TTS contract。

#### Edge TTS

当 `tts_provider: edge`，`:8001` 通过 `:9002` 调用 Edge TTS。它是在线 Provider，无需 API Key，但 synthesis 依赖公网。默认 voice 为 `zh-CN-XiaoxiaoNeural`。Edge 原生 MP3 会以 `audio/mpeg` 原样返回，Browser 当前 Blob/Audio 播放链可直接处理，不做额外 WAV 转码。

#### GSV-TTS-Lite

当 `tts_provider: gsv`，正式链路复用 Provider Runtime：

```text
Browser
  -> :8001/v1/tts
  -> :9002/v1/tts {provider=gsv}
  -> :9014/v1/tts
  -> GSV-TTS-Lite
  -> 32kHz WAV
```

GSV 的 voice 模型已经收敛为模板：

- project `.env` 只保存 global GPT/SoVITS model 路径和默认模板名；
- `voices/<name>.yaml` 是音色的唯一载体，保存 reference audio + exact transcript，并可按模板覆盖模型；
- `personas/<character>/voice.yaml` 只保存 `template: <name>` 引用，因此多个 Character 可以共享同一个音色。

Browser 仍把 Character id 作为 GSV voice 请求发送。sidecar 先解析角色引用，再落到对应模板；无法解析时才使用 `GSV_TTS_VOICE` 指定的默认模板。VoiceDesign freeze 会保存实际试听的 WAV、创建/更新模板、让角色引用该模板，再调用 `:9014/v1/voices/reload`。registry reload 不卸载已经热身的 GPT/SoVITS 权重。

`character-stack` 在独立 GSV venv 存在时启动 `:9014`。base model 或默认模板尚未配置完整时 sidecar 可以保持 listening/`ready=false`，Settings Center 可随后填写并通过 `/v1/configure` 热配置，不需要先用 shell export，也不会阻止整个 stack 启动。

#### Sherpa

当 `tts_provider: sherpa`，`:8001` 直接使用本地 Sherpa VITS。兼容 voice 为 `0 / 2 / 5`。

Workbench 的 Sherpa audition 使用：

```text
POST :8001/v1/providers/sherpa/tts
```

这个 endpoint 强制调用底层 Sherpa，不读取当前正式 Provider；因此正式配置为 GSV/Kokoro/Edge 时也不会出现“界面显示 Sherpa、实际听到别的 Provider”的假试听。

#### Workbench choice is not production config

`:9002/tts` 的 Provider/Voice 下拉只用于试听。正式默认值在 Settings Center 修改。

热生效边界：

- Provider / Voice / Speed：下一次正式 TTS 请求即生效；
- GSV asset / reference / device：通过 `:9014` reload 生效；
- Kokoro device：模型属于 `:9002` 进程，切 CPU/CUDA 后重启 `:9002`；
- Sherpa device：模型属于 `:8001` 进程，切 CPU/CUDA 后重启 `:8001`；
- Edge：cloud-managed，没有本地 Device reload。

因此不再要求为了普通 TTS 设置修改而重启整个 stack。

### 6. ASR configuration

`dev_stack.py` 会为本地模型设置默认路径。独立运行时可使用环境变量覆盖，例如：

```bash
export CHARACTER_MEDIA_ASR_MODEL="$PWD/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx"
export CHARACTER_MEDIA_ASR_TOKENS="$PWD/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/tokens.txt"
export CHARACTER_MEDIA_ASR_DEVICE=cpu
export CHARACTER_MEDIA_ASR_THREADS=2
export CHARACTER_MEDIA_ASR_LANGUAGE=auto
```

ASR 默认语言保持 `auto`。只有明确 benchmark/识别质量证明固定语言更优时，再本地 override。

采集回看（诊断用，默认关闭）：

```bash
export CHARACTER_MEDIA_ASR_CAPTURE_DIR="$PWD/data/asr-capture"
```

设置该变量只改变存储位置；**是否录制由测试态开关决定，默认关闭**，关闭时不写入任何文件。详见 `DEV_CONSOLE.md` → ASR Capture Review。

### 7. Windows native runtime safety

Windows 上最重要的约束不是“ONNX 能 import 就行”，而是必须加载**当前 venv 与 sherpa wheel 匹配的 native runtime**。

启动路径会：

1. 优先项目 `.venv/Scripts`；
2. 使用 `os.add_dll_directory` 加入合法 DLL 目录；
3. 尝试 preload venv 中匹配的 `onnxruntime.dll`；
4. 拒绝静默使用 `C:\Windows\System32\onnxruntime.dll` 作为 fallback。

不要通过替换 System32 DLL 解决项目依赖问题。

### 8. HTTP contracts

Media Runtime：

```text
GET  /health
POST /v1/asr
POST /v1/tts
POST /v1/providers/sherpa/tts
GET  /v1/metrics/recent
GET  /v1/dev/asr-capture
GET  /v1/dev/asr-capture/{id}/audio
POST /v1/dev/asr-capture/test-mode
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

`/v1/asr` 接受可选的 `X-ASR-Source` 请求头（`call` / `dictation`），仅用于给采集回看记录加来源标签。不发送该头不影响识别，记录里只是标为 `unknown`。

### 9. Voice interaction model — Pipeline V1.1

当前 Voice 仍然**不是 full-duplex Voice Agent**，也没有 barge-in；但 microphone capture 已与 TTS playback 状态解耦。

#### 9.1 TTS pipeline

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

#### 9.2 ASR stays live during TTS

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

#### 9.3 Capture state vs UI state

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

#### 9.4 Transcript validity gate

```text
empty / punctuation-only   -> reject
any Han character          -> accept
ASCII Latin/digit >= 2     -> accept
otherwise                  -> reject
```

无效 transcript 不创建 chat message，也不上传当前通话中的 Visual Capture frames。

### 9.5 Voice messages

Voice Message 与 Voice Call 共用正式 TTS Provider，但生命周期独立。当前主线已经完成 Direct/Group 的 `VOICE_MESSAGE` durable flow：先持久化 pending 文本事件，再由 materializer 调用正式 `:8001/v1/tts`、保存 WAV/MP3 MediaAsset、把同一事件更新为 ready/failed，并由 Browser voice bubble 合并状态和播放。

普通 `MESSAGE` 不会因为 Voice Call 开启就自动转成 durable Voice Message；两者不要依赖彼此的 UI session state。完整边界见 [CONVERSATION_RUNTIME.md#voice-messages](CONVERSATION_RUNTIME.md#voice-messages)。

### 10. Resource policy

- 主 LLM / Vision 保持 cloud-only。
- ASR/TTS 可 CPU 运行；GPU 是 benchmark 证明有收益后的 accelerator，不是默认假设。
- `tts_device: cuda` 只在当前 Torch build 真正暴露 CUDA 时有效。
- ASR 与 TTS 可以请求级重叠；不要把这扩张成无限并发 media inference。
- Browser Voice 自己把 TTS inference 串行为 1，并只预取下一条。
- ASR/TTS combined VRAM hard target 仍以轻量为目标；不为了“有 GPU”强制常驻所有模型。
- 模型 lazy load 是合理策略；不要因为 health probe 就强制加载全部本地模型。
- CI 不下载真实模型、不要求 GPU。

### 11. Benchmarks

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

### 12. Test layers

#### CI contract tests

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

#### Local real-model test

验证本机：

- cold/warm load
- inference latency
- Mandarin quality
- DLL/device
- CPU/GPU resource
- Kokoro formal routing
- speaker/microphone echo cancellation during real TTS playback
- no audible gap between prefetched group replies when synthesis finishes in time

#### Dev Console / TTS Lab

`:8002/dev` 验证正式服务链路；`:9002/tts` 用于多 Provider audition/benchmark。两者用途不同。

### 13. Current non-goals

当前仍未把以下能力作为稳定 contract：

- WebRTC full duplex
- barge-in / user speech interrupting TTS
- streaming ASR partials
- streaming TTS chunks
- sentence-level TTS pipeline
- general-purpose automatic voice cloning outside the explicit VoiceDesign/template workflow
- emotion/prosody control
- group call transport
- character-initiated call
- persistent raw audio

如果后续进入这些能力，应继续保持“Media 是渠道，Person 只有一个”的边界。

## TTS Workbench / Provider Runtime

`:9002` is both the TTS Provider Runtime used by formal routing and the browser Workbench used for audition/benchmark and VoiceDesign tooling.

The combination is intentional for V1. Provider lifecycle and Workbench UI can be split later only if their operational requirements actually diverge.

### 1. Runtime map

```text
Character Runtime          :8000
Media Runtime              :8001
Dev Console                :8002
Settings Center            :8003
TTS Workbench/Providers    :9002
CosyVoice optional         :9012
Qwen3-TTS 0.6B experiment  :9013 (manual only)
GSV-TTS-Lite               :9014
Qwen3 VoiceDesign          :9015 (manual/optional tool)
```

Formal realtime Providers are defined once in `character_memory.tts_registry`:

```text
kokoro
sherpa
edge
gsv
```

Qwen3-TTS 0.6B is not in that registry and has no realtime Provider adapter in `:9002`. Qwen3 1.7B VoiceDesign is a tool, not a chat Provider.

CosyVoice remains Workbench-only/experimental.

### 2. Stable browser contract

The chat UI never calls provider-specific endpoints:

```text
Browser
  -> POST :8001/v1/tts
  -> Media Runtime reads current config.yaml selection
```

Routes:

```text
sherpa -> local Media Runtime VITS
kokoro -> :9002/v1/tts -> Kokoro
edge   -> :9002/v1/tts -> Edge online TTS
gsv    -> :9002/v1/tts -> :9014 GSV-TTS-Lite
```

Workbench selection is audition-only and does not change formal chat configuration. Production selection is changed in Settings Center.

Per-provider status, synthesis timing and A/B results reach the page as raw JSON only inside the collapsed `<details class="debug-output">` blocks from `ui.css`; the badge, the device/inference line and the audio player stay visible without a click.

Provider/Voice/Speed are hot on the next formal synthesis request. Device semantics are provider-specific:

- GSV device can be reconfigured/reloaded through `:9014`;
- Kokoro device belongs to the running `:9002` process;
- Sherpa device belongs to the running `:8001` process;
- Edge is cloud-managed.

Changing a non-hot local device therefore requires restarting only the corresponding TTS runtime, not the whole Character Memory stack.

### 3. Sherpa audition isolation

Sherpa Workbench audition deliberately does **not** call the formal `:8001/v1/tts` selector. It uses:

```text
POST :8001/v1/providers/sherpa/tts
```

This route always invokes the underlying local Sherpa VITS runtime. Therefore selecting “Sherpa” in the Workbench cannot accidentally synthesize GSV/Kokoro/Edge merely because one of those is the formal chat Provider.

The same loaded Sherpa runtime is reused; no second model copy is created.

Current Sherpa speakers:

```text
0
2
5
```

### 4. Health inventory and Settings

Settings probes providers independently:

```text
GET :9002/v1/providers/kokoro
GET :9002/v1/providers/sherpa
GET :9002/v1/providers/edge
GET :9002/v1/providers/gsv
```

A failed optional Provider cannot hide healthy Providers. Health reports:

- `ready` / `loaded`;
- voices and default voice;
- model/device;
- speed support;
- failure reason.

Settings rebuilds its Provider/Voice controls from this inventory and repeats server-side validation on save.

### 5. Canonical setup

```bash
bash scripts/setup-media-models.sh
```

This command:

1. runs the canonical dependency sync;
2. prefetches the local BGE embedding model;
3. prepares SenseVoice ASR;
4. prepares Sherpa VITS;
5. prefetches Kokoro model/voice assets.

Normal Character Runtime embedding is strict-offline, so network model acquisition belongs here rather than in startup/first chat.

If `uv.lock` is present, `scripts/sync-all.sh` uses `uv sync --locked`; otherwise it resolves the declared dependency graph. The repository does not fabricate a lockfile.

### 6. Kokoro

Model:

```text
hexgrad/Kokoro-82M-v1.1-zh
```

Voices:

```text
zf_001
zf_002
zf_003
zf_004
```

Model/config/voice assets are prefetched under the local Hugging Face cache. Request-time synthesis does not download missing assets; a missing cache produces an explicit unavailable state.

Kokoro model device is selected when the `:9002` runtime instantiates the model. Changing `tts_device` between CPU/CUDA is persisted but requires restarting `:9002`.

### 7. Edge TTS

Edge runs inside `:9002` and is online-only. It needs no API key but synthesis requires Microsoft Edge TTS network access.

Default voice:

```text
zh-CN-XiaoxiaoNeural
```

Other configured Chinese voices come from the central registry. Edge returns MP3 and the Media/Browser chain preserves `audio/mpeg`; no unnecessary WAV transcode is inserted.

Health only verifies that the client dependency exists; it does not make a public-network call on every health probe.

### 8. GSV-TTS-Lite

GSV uses its isolated Python/CUDA sidecar:

```text
:9002
  -> :9014
  -> .external/GSV-TTS-Lite/.venv
```

Global/default runtime assets are managed through Settings Center and persisted in project `.env`:

```text
GSV_TTS_GPT_MODEL
GSV_TTS_SOVITS_MODEL
GSV_TTS_VOICE
```

`GSV_TTS_VOICE` names the default template, not a reference clip: each template under `voices/<name>.yaml` carries its own `ref_audio` and `ref_text`.

Manual `export GSV_TTS_...` remains valid only as an explicit system/deployment override or when launching the sidecar by hand. It is not the normal application workflow.

GSV exposes runtime operations used by Settings:

```text
POST :9014/v1/configure
POST :9014/v1/load
POST :9014/v1/unload
POST :9014/v1/voices/reload
```

When selected, Settings preloads GSV. Switching away unloads it to release VRAM.

#### Voice templates and the character registry

A voice is a template, and a character only names one:

```text
voices/<name>.yaml                    # ref_audio + ref_text: the only carrier
voices/<name>/<content-addressed>.wav
personas/<character>/voice.yaml       # one line: template: <name>
```

The browser sends the Character id as the requested voice for GSV. If that id resolves in the GSV registry, the template it names is used; otherwise GSV falls back to the default template (`GSV_TTS_VOICE`) instead of muting the character.

VoiceDesign freeze stores the exact auditioned WAV and transcript as a template, rewrites the character's `voice.yaml` to name it, then asks the GSV sidecar to reload the registry without unloading the warm GPT/SoVITS engine.

Detailed behavior and determinism measurements live in `VOICE_AND_TTS.md`.

### 9. Qwen3 boundary

The old Qwen3-TTS 0.6B sidecar and benchmark scripts remain for manual experimentation, but there is no `Qwen3SidecarProvider` in the Workbench runtime and `qwen3` cannot be selected as formal chat TTS.

Qwen3 1.7B VoiceDesign at `:9015` is a separate tool. Workbench prompt polish uses the public `PersonModel.complete_text_for_session()` contract and the normal configured Character Memory LLM; it does not depend on the OpenAI adapter's private transport method.

VoiceDesign is **explicit opt-in tooling**. `character-stack` does not auto-start `:9015`, and ordinary Character / Ensemble creation must not silently invoke it. One-prompt Ensemble exposes a single optional “为新角色生成专属音色” choice; only when the user selects it **and** the manually-started VoiceDesign sidecar reports ready does the background design flow run. Its instruct is derived from identity / personality / speech style rather than depending on an exact numeric age. Generation/freeze remains best-effort: unavailable sidecar, CUDA/model errors or a single failed character never roll back the already-created Character or Group, and the existing default/fallback voice remains valid.

The freeze flow is described in this document and the One-prompt Ensemble lifecycle is owned by `CONVERSATION_RUNTIME.md`.

### 10. CosyVoice

CosyVoice stays isolated in its Python 3.10 sidecar at `:9012`. It is not required by the main stack and is not a formal chat Provider. If absent, the Workbench shows it unavailable and all formal Providers remain usable.

### 11. Current synthesis policy

GSV currently returns whole WAV output through `infer_batched`; formal Character voice playback is not token/chunk streaming. Browser voice overlaps playback of the current message with synthesis of the next queued message, preserving one-at-a-time playback.

Real performance acceptance should use the actual formal path:

```text
Browser/:8001 -> :9002 -> provider
```

and distinguish model inference, HTTP time, audio duration, RTF and first-playable-audio latency where applicable.

### 12. Testing boundary

CI verifies:

- formal provider registry/config validation;
- `:8001` formal routing;
- direct Sherpa audition bypassing the formal selector;
- Settings Provider/Voice/Device browser interaction;
- provider API shapes and optional-provider isolation;
- VoiceDesign freeze/registry contracts.

CI does not prove subjective voice quality or real CUDA latency. Those remain Windows/GPU acceptance tests.

## GSV-TTS-Lite

Status: available in both TTS Lab and the formal `:8001/v1/tts` route. V1 synthesizes whole WAVs; every voice is a template under `voices/`.

### Architecture

Character Memory keeps GSV-TTS-Lite isolated from the main Python environment:

    Browser -> Media Runtime :8001
      -> :9002 TTS Provider Runtime + Lab
      -> GSV sidecar :9014
      -> external GSV-TTS-Lite Python 3.12 CUDA environment
      -> full WAV response

The browser still knows nothing about the GSV-specific sidecar. It always calls `:8001/v1/tts`; Media Runtime routes `tts_provider=gsv` through `:9002` to `:9014`.

### Local runtime

Expected source/runtime layout:

    .external/GSV-TTS-Lite/
      gsv_tts/
      .venv/

The external directory, CUDA Torch environment, model weights and reference audio are local assets and must not be committed.

Required environment variables:

    GSV_TTS_GPT_MODEL
    GSV_TTS_SOVITS_MODEL

The default template must also resolve: `voices/<GSV_TTS_VOICE>.yaml` under
`GSV_TTS_VOICES_ROOT`, whose `ref_audio` file exists. The reference clip and its
transcript live in that template, not in the environment.

Optional environment variables:

    GSV_TTS_ROOT
    GSV_TTS_VENV
    GSV_TTS_HOST
    GSV_TTS_PORT
    GSV_TTS_DEVICE
    GSV_TTS_MODELS_DIR
    GSV_TTS_VOICE
    GSV_TTS_VOICES_ROOT
    GSV_TTS_PERSONA_ROOT
    GSV_TTS_LANGUAGE
    GSV_TTS_PROMPT_LANGUAGE
    GSV_TTS_PRELOAD
    GSV_TTS_SEED

Defaults:

    host = 127.0.0.1
    port = 9014
    device = cuda
    voice = murasame
    voices_root = <repo>/voices (pinned absolute by scripts/start-gsv-tts.sh and the dev stack)
    language = zh
    prompt_language = auto
    preload = 1 when scripts/start-gsv-tts.sh is used
    seed = 1234

### Start

Use Windows-style forward-slash paths for local model assets when launching from Git Bash, for example:

    export GSV_TTS_GPT_MODEL="C:/path/to/voice.ckpt"
    export GSV_TTS_SOVITS_MODEL="C:/path/to/voice.pth"
    export GSV_TTS_VOICE="murasame"

    bash scripts/start-gsv-tts.sh

Only the two base models are exported: the reference clip is a template. The
default template `voices/murasame.yaml` holds it, with `ref_audio` relative to
its own directory:

    ref_audio: murasame.wav
    ref_text: <exactly what the clip says>

`scripts/start-gsv-tts.sh` pins `GSV_TTS_VOICES_ROOT` to `<repo>/voices` (the
same absolute-path rule as `GSV_TTS_PERSONA_ROOT`), so a template created in TTS
Lab is found without further configuration. `/health` reports `ready=false` and
names the missing template or clip until one exists.

Health:

    http://127.0.0.1:9014/health

Then start the Lab in the normal environment:

    uv run character-tts-lab

Open:

    http://127.0.0.1:9002/tts

### Sidecar contract

Endpoints:

    GET  /health
    POST /v1/load
    POST /v1/tts

TTS request:

    {
      "text": "你好",
      "voice": "murasame",
      "language": "zh",
      "speed": 1.0
    }

Response is audio/wav and exposes the same measurement header family used by the Qwen3 sidecar:

    X-TTS-Provider
    X-TTS-Voice
    X-TTS-Model
    X-TTS-Device
    X-TTS-Inference-Ms
    X-TTS-Audio-Ms
    X-TTS-RTF
    X-TTS-Sample-Rate
    X-TTS-Cuda-Allocated-MB
    X-TTS-Cuda-Reserved-MB
    X-TTS-Cuda-Peak-MB
    X-TTS-Seed

### Determinism

GSV-TTS-Lite draws every random number from PyTorch's **global** RNG and accepts no
`generator=` anywhere — token sampling (Gumbel-max, `GPT_SoVITS/GPT/utils.py:5-9`) and
decoder noise (`GPT_SoVITS/SoVITS/models.py:404`) both. The sidecar therefore calls
`torch.manual_seed()` immediately before `infer_batched`.

**It must run inside the sidecar's `RLock`.** Seeding outside the lock lets another
thread consume the RNG state between the seed and the inference, which silently
restores the non-determinism it was meant to remove. `tests/test_gsv_tts_experiment.py`
pins this ordering, not just the seed's existence.

`GSV_TTS_SEED` (default `1234`) sets the runtime default. `"none"`, `"off"`, `"random"`
and `"-1"` disable seeding and restore the old behaviour. A request may carry its own
`seed`; `null` means "use the runtime default".

Measured on the real HTTP path, same 25-character text, same process, 5× `POST :9014/v1/tts`:

| | duration | waveform |
|---|---|---|
| before | 25–88 % spread | 5/5 distinct |
| after (`seed=1234`) | **6720.0 ms, 5/5** | 4/5 byte-identical |

Run 1 differs from runs 2–5 only by first-inference cuDNN kernel warm-up float noise:
corr 0.999986, SNR 45.7 dB, max abs diff 0.96 % FS — inaudible. Expect one slightly
different waveform after a fresh model load, then exact repeats.

#### Seed choice is not a fidelity lever

A two-text sample suggested seed 0 was best (0.7989) and 1234 worst (0.7342). **Five
fresh texts did not reproduce it** — all three seeds landed within 0.007. Pooled over
n=7, via Eres2Net speaker similarity:

| seed | mean | min | max |
|---|---|---|---|
| 0 | 0.7552 | 0.6509 | 0.8306 |
| 99999 | 0.7445 | 0.5796 | 0.8359 |
| 1234 | 0.7333 | 0.6787 | 0.8193 |

Between-seed spread is 0.0218; between-sentence spread is 0.2563 — **~12× larger**. What
is said dominates identity, not the seed. Do not build a "scan seeds, keep the best
score" flow: that sells a measurement that does not replicate. The seed's job is
**reproducibility**. Choose its value by pacing (the same short line ranges 1276–11520 ms
across seeds — some seeds stretch three characters into 11.5 s) and by ear.

`temperature`/`top_k`/`top_p`/`repetition_penalty`/`noise_scale` are deliberately **not**
exposed per request. They are the other end of the same lever as the seed; making them
per-request would hand every caller a knob that destroys the reproducibility just gained.
If timbre ever needs tuning, pin per-voice values in the template instead.

### V1 inference policy

V1 deliberately uses GSV-TTS-Lite infer_batched and returns a complete WAV.

The locally measured whole-sentence latency is already low enough to validate the product path without adding streaming protocol or browser playback complexity. TTFT/infer_stream remains a follow-up experiment only if real chat audition shows a first-sentence problem.

At load time the sidecar:

1. creates the GSV TTS engine;
2. loads the configured GPT model;
3. loads the configured SoVITS model;
4. pre-caches speaker audio;
5. pre-caches prompt audio/text.

scripts/start-gsv-tts.sh enables preload by default so the first Lab request does not pay model load cost.

### Current boundary

Formal GSV routing now has two voice layers:

- `config.py` / Settings select `tts_provider: gsv` and the global/default formal voice;
- Media Runtime routes GSV through `:9002` to the isolated `:9014` sidecar;
- Settings owns the global/default GSV GPT/SoVITS models and the default template name, and can hot-configure/reload `:9014`;
- `voices/<name>.yaml` owns a reference clip and its transcript — the only place either lives;
- `personas/<character>/voice.yaml` optionally names one (`template: <name>`), so a character can be given a voice and can share it;
- Browser voice requests carry the Character id. A registered id uses the template it names; an unknown id degrades to the default template;
- VoiceDesign freeze stores the exact auditioned WAV bytes as a template (`voices/<character id>.yaml` plus its content-addressed clip), rewrites `voice.yaml` to name it, then calls `POST /v1/voices/reload`.

`GSV_TTS_VOICE` names the **default template** — the one every unresolvable voice request lands on. It does not replace the per-character registry.

`config.yaml`'s `tts_voice` does **not** select that default. It is a cross-provider field (a kokoro voice name, a sherpa speaker id) and stays one, so on the GSV path it is not what decides an unconfigured character's voice: the browser always sends `voice: <character id>`, and a name that does not resolve falls to `GSV_TTS_VOICE`.

Registry reload deliberately keeps the warm engine resident. A template may override GPT/SoVITS models; unset template model fields inherit the global runtime models.

Still out of scope: SSE/WebRTC/token-level browser streaming and a general model-asset management system. Qwen3 VoiceDesign remains optional tooling, not a prerequisite for GSV and not a formal chat Provider.


### Runtime configuration ownership

`config.yaml` does not own GSV model assets. It only selects the formal TTS provider/voice/speed/device.

The three GSV runtime values are persisted in the adjacent project `.env` through Settings Center:

```text
GSV_TTS_GPT_MODEL
GSV_TTS_SOVITS_MODEL
GSV_TTS_VOICE
```

`GSV_TTS_VOICE` is a template name, chosen from a dropdown of what `voices/`
actually contains; the reference clip is not a setting at all.

The sidecar can start with these fields missing and report `ready=false`. Settings Center may then configure the running sidecar through `POST /v1/configure`; no full-stack restart is required. Model/device changes unload and reload the GSV engine when needed, while switching away from GSV uses `POST /v1/unload` to release GPU memory. Adding or editing a template is a file change, not an engine change: `POST /v1/voices/reload` re-reads both trees and keeps the warm weights.

The project launcher does not inject the whole project `.env` into every child process. Only the GSV child receives its `GSV_TTS_*` values as process environment because that upstream runtime consumes environment variables directly. This preserves the intended precedence `real system env > project .env` and prevents Settings edits from being masked by stale launcher-copied values.

## Qwen3-TTS isolated runtime

Qwen3-TTS is **not** a formal realtime Character Memory TTS provider. Its Torch/CUDA runtime stays isolated in a dedicated sidecar for experiments and future voice-design tooling.

Current product position: the existing 0.6B CustomVoice / optional Base clone code remains experimental infrastructure. Qwen3-TTS is excluded from Settings, formal `tts_provider`, Media Runtime chat routing and the normal `character-stack`. Qwen3-TTS 1.7B VoiceDesign now has a dedicated Workbench UI + adapter contract, but the heavy local runtime remains external and must be connected separately.

### Scope

- isolated sidecar on 127.0.0.1:9013
- isolated Python 3.12 environment: .venv-qwen3-tts
- default model: Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
- default attention: SDPA
- default dtype: auto; CUDA prefers BF16 when supported and otherwise falls back to FP32; CPU uses FP32
- explicit model load endpoint and lazy first-request loading
- latency, RTF and VRAM benchmark
- optional Base-model voice-clone mode
- current CustomVoice request contract already supports `instruct`, but the TTS Lab UI does not expose a voice-style prompt field yet

It is deliberately **not** wired into formal chat configuration or the normal Character Memory stack.

### Install

From Git Bash:

    bash scripts/setup-qwen3-tts.sh --prefetch

This creates .venv-qwen3-tts, installs the official qwen-tts==0.1.1 package and sidecar dependencies, prints Torch/CUDA/GPU information, and prefetches the default 0.6B CustomVoice checkpoint into models/huggingface.

To prefetch the Base checkpoint instead:

    bash scripts/setup-qwen3-tts.sh --prefetch --model Qwen/Qwen3-TTS-12Hz-0.6B-Base

If the environment reports cuda_available: False, fix Torch/CUDA in the isolated environment before judging latency. Do not install Torch into the main Character Memory environment just for this experiment.

### Start the sidecar

    bash scripts/start-qwen3-tts.sh --preload

Default URL is http://127.0.0.1:9013.

Endpoints:

    GET  /health
    POST /v1/load
    POST /v1/tts

Omit --preload when you want the first /v1/load call to measure cold model loading.

Useful overrides:

    QWEN3_TTS_DEVICE=cuda:0 QWEN3_TTS_DTYPE=auto QWEN3_TTS_ATTN=sdpa bash scripts/start-qwen3-tts.sh

For CUDA GPUs with BF16 support, `auto` resolves to `bfloat16`, matching the upstream Qwen3-TTS inference examples. Explicit FP16 is not the formal default because it can trigger unstable generation on some GPUs.

FlashAttention 2 is deliberately not installed by the setup script. Establish a clean SDPA baseline first.

### Benchmark

In another Git Bash:

    bash scripts/benchmark-qwen3-tts.sh --repeats 10

The benchmark records cold model load time, load-time CUDA peak allocation, short/medium/long Chinese replies, HTTP end-to-end latency, model inference latency, generated audio duration, RTF, P50/P95, request peak VRAM, and resident VRAM after the test.

Artifacts are written to data/qwen3-tts-benchmark/:

    short.wav
    medium.wav
    long.wav
    benchmark.json

The WAV files are kept so latency and subjective voice quality can be reviewed together.

### Built-in Chinese voices

The default 0.6B CustomVoice checkpoint includes Vivian, Serena, Uncle_Fu, Dylan and Eric.

Example:

    bash scripts/benchmark-qwen3-tts.sh --voice Serena --repeats 10

### Optional 0.6B Base voice clone

The same sidecar supports the Base checkpoint. Start it with a reference WAV:

    QWEN3_TTS_MODEL=Qwen/Qwen3-TTS-12Hz-0.6B-Base QWEN3_TTS_REF_AUDIO=/c/path/to/reference.wav QWEN3_TTS_REF_TEXT='参考音频对应的准确文本。' bash scripts/start-qwen3-tts.sh --preload

If QWEN3_TTS_REF_TEXT is omitted, the sidecar builds an x-vector-only clone prompt. This is simpler but may reduce clone fidelity.

### VoiceDesign status — Workbench contract reserved

VoiceDesign is no longer treated as a realtime Provider and is not part of normal stack startup. Instead, TTS Workbench exposes a separate Voice Design tool surface.

Implemented in Character Memory:

- Voice Design UI in `:9002/tts`;
- sidecar status adapter;
- generate proxy;
- AI prompt polish using the standard Character Memory LLM configuration;
- fixed local sidecar contract at `:9015` by default.

Not implemented in Character Memory main environment:

- 1.7B model download;
- qwen-tts/Torch installation for VoiceDesign;
- CUDA preload;
- local sidecar process startup.

Local integration details are specified in `docs/current/VOICE_AND_TTS.md`.

### Acceptance questions

For production acceptance, answer these questions with real data:

1. Does the 0.6B model fit without OOM while the intended application workload is present?
2. What is resident VRAM after load?
3. What are warm P50/P95 inference latency and RTF for normal 15-30 character Chinese replies?
4. Does SDPA already meet latency requirements, or is FlashAttention worth the deployment complexity?
5. Is the audible improvement over the lightweight TTS route large enough to justify several GB of GPU residency?


### Current integration boundary

The sidecar code and setup/start scripts remain available for explicit experiments, but normal Character Memory startup does not launch Qwen3-TTS and formal chat cannot select it.

If VoiceDesign work is resumed later, build a dedicated tool/workflow around the isolated runtime instead of reintroducing it as a realtime chat provider.

## Qwen3-TTS 1.7B VoiceDesign Workbench

Status: complete. UI, the Character Memory adapter, and the `:9015` sidecar
(`src/character_memory/qwen3_voice_design_experiment.py`) are all implemented and
verified end-to-end. The sidecar is intentionally **not** auto-started by the main
stack — see "Starting the sidecar" below.

### Product role

Qwen3-TTS 1.7B VoiceDesign is a **voice creation tool**, not a realtime chat TTS provider.

It must not appear in:

- `config.yaml: tts_provider`;
- Settings Center realtime Provider selector;
- Media Runtime formal chat routing;
- realtime A/B Provider list.

It lives in the TTS Workbench as a separate tool:

```text
TTS Workbench :9002/tts
  -> Voice Design panel
  -> :9002/v1/voice-design/*
  -> optional local sidecar :9015
  -> Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign
```

Normal `character-stack` does not start or preload the 1.7B model.

### Starting the sidecar

```bash
bash scripts/start-qwen3-voice-design.sh          # :9015, lazy — loads on first request
```

Measured on an RTX 5060 Laptop (8 GB), 2026-09-19:

| | value |
|---|---|
| model load | ~24 s |
| resident after load | 3994 MB allocated / 4132 MB reserved (4515 MiB process) |
| first generation after load | 22.8 s for 1.68 s audio (**warm-up: kernel autotune**) |
| steady state | 5.8–7.0 s for ~2.0 s audio, **RTF ~2.9–3.5** |
| `POST /v1/unload` | releases to 709 MiB; ~455 MB stays as the process CUDA context |

**The first generation in a session is 3–4× slower than the rest.** Warm the model
once before showing the panel to someone, or expect a ~23 s first click.

`/v1/unload` genuinely returns the card, which matters because the 1.7B model
cannot be resident together with GSV on an 8 GB card. GSV reloads in ~6 s
afterwards and works normally while an *idle* VoiceDesign process is still
running, so the steady state is: GSV loaded, VoiceDesign process up but unloaded.

#### Endpoints

```text
GET  :9015/health           -> 200 always; {ready, loaded, model, device, reason}
POST :9015/v1/voice-design  -> WAV + X-Voice-Design-{Model,Device,Inference-Ms,Audio-Ms,Sample-Rate}
POST :9015/v1/load          -> explicit preload
POST :9015/v1/unload        -> release VRAM before switching back to GSV
```

There is deliberately **no** `/v1/configure`: there is one model, and its id is a
contract constant on both sides. Model choice belongs to `QWEN3_VOICE_DESIGN_MODEL`.
`/health` returns 200 even when `ready: false`, because the adapter reads a non-2xx
status as "sidecar not running" and would show the wrong remedy.

Designs are **not reproducible**: the same text and instruct gave 1.68 s, 1.92 s and
2.00 s of audio across four identical requests. Keep the WAV — do not expect to
regenerate the same voice.

### Workbench HTTP surface

Character Memory owns these stable endpoints:

```text
GET  :9002/v1/voice-design/status
POST :9002/v1/voice-design/polish
POST :9002/v1/voice-design/generate
```

#### Status

`GET /v1/voice-design/status` returns the sidecar state without registering it as a TTS Provider.

Example:

```json
{
  "voice_design": {
    "id": "qwen3-voice-design",
    "label": "Qwen3-TTS 1.7B VoiceDesign",
    "ready": true,
    "loaded": true,
    "model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "device": "cuda:0",
    "reason": null,
    "base_url": "http://127.0.0.1:9015"
  }
}
```

#### AI prompt polish

`POST /v1/voice-design/polish`:

```json
{
  "description": "年轻一点，温柔，软一点，不要太嗲，有一点慵懒感",
  "language": "Chinese"
}
```

Response:

```json
{
  "ok": true,
  "instruct": "年轻女性声线，音色清亮柔和，略带慵懒感……",
  "model": "deepseek-flash",
  "total_ms": 1234.5
}
```

This endpoint reuses the standard Character Memory LLM configuration:

```text
OPENCODE_GO_API_KEY
base_url
chat_model
```

No second API key or LLM configuration is introduced. Workbench calls the public `PersonModel.complete_text_for_session()` contract; it does not reach into the OpenAI-compatible adapter's private `_request()` transport method.

#### Generate

`POST /v1/voice-design/generate`:

```json
{
  "text": "你好，这是 Character Memory 的新声线试听。",
  "language": "Chinese",
  "instruct": "年轻女性声线，音色清亮柔和，略带慵懒感。",
  "max_new_tokens": 2048
}
```

The response is audio bytes, normally `audio/wav`, with normalized Workbench headers:

```text
X-Voice-Design-Model
X-Voice-Design-Device
X-Voice-Design-Inference-Ms
X-Voice-Design-Audio-Ms
X-Voice-Design-Sample-Rate
X-Voice-Design-Artifact
```

`X-Voice-Design-Artifact` is an opaque, server-generated token naming *this*
audition. It is the only handle accepted by `freeze` (below). The caller never
chooses it and it is not derived from the audio, so two identical auditions get
two distinct tokens. The Lab keeps the last **8** auditions; older ones are
evicted and freezing them returns 404.

#### Freeze

`POST /v1/voice-design/freeze` turns an audition into a character's permanent voice:

```json
{ "character_id": "momo", "artifact_id": "5qlrisDLQfsUddO9ONOTvQ" }
```

The request is `extra="forbid"` and takes **no** text or audio: the reference
transcript is the text that actually produced the audition, so a client cannot
smuggle in a transcript that does not match the audio. That mismatch is the
hardest failure mode of zero-shot cloning, and this design removes it by
construction rather than by validation.

On success it writes, beside the character's `persona.yaml`:

```text
personas/<id>/voice/<sha256[:16]>.wav   # the auditioned bytes, verbatim
personas/<id>/voice.yaml                # the profile
```

The WAV is content-addressed, so re-freezing never overwrites an earlier clip.
That matters because **VoiceDesign is unseeded** — four identical requests
produce four different clips, so a frozen clip cannot be regenerated from its
prompt. It is an asset, not a recipe.

`voice.yaml`:

```yaml
voice_id: momo            # omit to default to the directory name
ref_audio: voice/ffa77b6a3acab112.wav   # relative to this file
ref_text: 你好，今天天气不错，我们出去走走吧。
gpt_model: null           # null inherits the global GSV model
sovits_model: null
## provenance, written by freeze; GSV does not read these, but a clip that
## cannot be regenerated is only explainable through its record:
created_at: '2026-09-19T15:26:34.472897+00:00'
instruct: 年轻女性，声音清亮柔和，语速偏慢，带一点温柔的笑意。
model: Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign
```

> The reader (`voices.py`) declares the provenance fields explicitly and keeps
> `extra="forbid"`, so a typo in a *contract* field still fails loudly. The two
> schemas must change together — see
> `tests/test_tts_lab_voice_freeze.py::test_frozen_voice_yaml_is_loadable_by_the_registry`,
> which round-trips the writer through the reader.

After writing, freeze asks the GSV sidecar to re-read the registry:

```text
POST http://127.0.0.1:9014/v1/voices/reload
```

This is a pure file re-read: no engine reload, no VRAM movement. New reference
audio is cached lazily on the next `infer_batched`, so **the voice is live on the
next chat turn**. The response reports what happened:

```json
{ "ok": true, "character_id": "momo", "voice_id": "momo",
  "ref_audio": "voice/ffa77b6a3acab112.wav", "ref_text": "你好…",
  "activated": true, "reason": null }
```

**Reload failure is non-fatal by design.** If GSV is not running, freeze still
writes both files and answers 200 with `activated: false` and a reason; the voice
registers on the next GSV start. Files are never half-written: the WAV lands
first, `voice.yaml` last, so a crash cannot leave a profile pointing at a missing
clip.

### Local sidecar contract

The local implementation should bind by default to:

```text
http://127.0.0.1:9015
```

Override from Character Memory with:

```text
CHARACTER_TTS_QWEN3_VOICE_DESIGN_BASE
```

The sidecar needs the two contract endpoints below, plus `/v1/load` and `/v1/unload`
for VRAM control (see "Starting the sidecar").

#### GET /health

Recommended response:

```json
{
  "ready": true,
  "loaded": true,
  "model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
  "device": "cuda:0",
  "reason": null
}
```

Semantics:

- `ready=true`: the sidecar can accept a generation request;
- `loaded=true`: the model is already resident;
- `reason`: human-readable failure reason when unavailable.

#### POST /v1/voice-design

Request body is exactly the Workbench generate payload:

```json
{
  "text": "待合成文本",
  "language": "Chinese",
  "instruct": "声线描述",
  "max_new_tokens": 2048
}
```

The local implementation should call the official VoiceDesign model conceptually as:

```python
wavs, sr = model.generate_voice_design(
    text=text,
    language=language,
    instruct=instruct,
    max_new_tokens=max_new_tokens,
)
```

Return WAV bytes. Preferred response headers:

```text
Content-Type: audio/wav
X-Voice-Design-Model: Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign
X-Voice-Design-Device: cuda:0
X-Voice-Design-Inference-Ms: 3200.0
X-Voice-Design-Audio-Ms: 2800.0
X-Voice-Design-Sample-Rate: 24000
```

For compatibility, Character Memory's adapter also accepts the existing `X-TTS-*` metric header names.

### Local implementation constraints

The local sidecar should remain isolated from the main Character Memory Python environment.

Recommended boundary:

```text
main Character Memory .venv
  - no qwen-tts
  - no VoiceDesign model
  - no extra Torch requirement

separate VoiceDesign environment
  - qwen-tts
  - CUDA/Torch
  - Qwen3-TTS 1.7B VoiceDesign assets
```

Runtime should be local/offline once assets are prepared. Do not make a generation request depend on Hugging Face network access. The local integration may choose its own model directory, dtype, attention implementation and preload policy.

### UI workflow

The Workbench flow is:

```text
raw voice description
  -> AI 润色 (optional)
  -> editable instruct
  -> test text + language
  -> Generate Voice Design
  -> audio audition + runtime metadata
```

The generated audio is an audition artifact until it is frozen. The Workbench
enables "freeze to character" only while the live inputs still match the
snapshot the audition was generated from; **Freezing** above persists the bytes
and registers them with GSV.

This matters more than it looks, because VoiceDesign does **not** accept a seed: four
identical requests produced 1.68 s, 1.92 s, 2.00 s and 2.00 s of audio. A designed
voice cannot be regenerated from its `instruct` — **the WAV is the asset.** Any freeze
flow must persist the bytes, not the prompt that produced them.

Timing, measured (RTX 5060 Laptop, 8 GB):

| step | value |
|---|---|
| first generation after a load | 22.8 s for 1.68 s audio — cuDNN kernel autotune |
| steady state | 5.8–7.0 s for ~2.0 s audio (**RTF ~2.9–3.5**) |
| `POST /v1/unload` | 4515 MiB → 709 MiB |

Budget the first click at ~23 s, not ~6 s.
