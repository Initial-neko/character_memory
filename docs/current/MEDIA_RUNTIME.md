# Media Runtime — Local ASR / TTS

Media Runtime 负责本地语音模型，与 Character Runtime 分离。

核心目标不是建立第二个 Voice Agent，而是：

```text
speech
  -> local ASR
  -> same Character Runtime
  -> same Person / Memory / Mental State
  -> local TTS
```

## 1. Architecture

```text
Browser microphone
  ↓ PCM16 WAV / VAD
Media Runtime :8001
  ↓ SenseVoice ASR
Character Runtime :8000
  ↓ POST /v1/chat/messages
  ↓ PersonRuntime / Cloud LLM
  ↓ SSE character_event
Media Runtime :8001
  ↓ VITS TTS
Browser playback
```

三个开发服务：

```text
Character Runtime :8000
Media Runtime     :8001
Dev Console       :8002
```

推荐统一启动：

```bash
bash scripts/sync-all.sh
uv run character-stack
```

独立诊断时：

```bash
uv run character-memory web --port 8000
bash scripts/run-media.sh
uv run character-dev
```

## 2. Ownership boundary

Character Runtime owns：

- Persona
- Event / Memory / Mental State
- cloud LLM / Vision
- conversation/SSE

Media Runtime owns：

- ASR model lifecycle
- TTS model lifecycle
- audio parsing/resampling
- local inference timings

Media Runtime 不 import / instantiate `PersonRuntime`。

停止 Media Runtime 不应让纯文本聊天不可用。

## 3. Dependencies

Canonical full-dev environment：

```bash
bash scripts/sync-all.sh
```

`pyproject.toml` 的 media stack 当前锁定：

```text
sherpa-onnx==1.13.5
sherpa-onnx-core==1.13.5
```

不要为了只安装 Media 依赖而在完整开发 venv 中反复运行不同的 `uv sync --extra ...` 组合；uv exact sync 会按当前声明集合移除其它 extra-only package。

CI 的 isolated job 可以按 job 需要选择 extras，这和本地完整开发环境不是同一场景。

## 4. Models

当前本地模型：

### ASR

```text
models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/
```

关键文件：

- `model.int8.onnx`
- `tokens.txt`

### TTS

```text
models/sherpa-onnx-vits-zh-ll/
```

关键文件：

- `model.onnx`
- `tokens.txt`
- `lexicon.txt`
- `dict/`
- rule FSTs

可使用：

```bash
bash scripts/setup-media-models.sh
```

或按 sherpa-onnx 官方 release 手工下载。

## 5. Environment configuration

典型 Git Bash：

```bash
export CHARACTER_MEDIA_ASR_MODEL="$PWD/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx"
export CHARACTER_MEDIA_ASR_TOKENS="$PWD/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/tokens.txt"
export CHARACTER_MEDIA_ASR_DEVICE=cpu
export CHARACTER_MEDIA_ASR_THREADS=2
export CHARACTER_MEDIA_ASR_LANGUAGE=auto

export CHARACTER_MEDIA_TTS_MODEL="$PWD/models/sherpa-onnx-vits-zh-ll/model.onnx"
export CHARACTER_MEDIA_TTS_TOKENS="$PWD/models/sherpa-onnx-vits-zh-ll/tokens.txt"
export CHARACTER_MEDIA_TTS_LEXICON="$PWD/models/sherpa-onnx-vits-zh-ll/lexicon.txt"
export CHARACTER_MEDIA_TTS_DICT_DIR="$PWD/models/sherpa-onnx-vits-zh-ll/dict"
export CHARACTER_MEDIA_TTS_RULE_FSTS="$PWD/models/sherpa-onnx-vits-zh-ll/phone.fst,$PWD/models/sherpa-onnx-vits-zh-ll/date.fst"
export CHARACTER_MEDIA_TTS_DEVICE=cpu
export CHARACTER_MEDIA_TTS_THREADS=2
```

ASR 默认语言保持 `auto`。只有明确 benchmark/识别质量证明固定语言更优时，再在本地配置 override。

## 6. Windows native runtime safety

Windows 上最重要的约束不是“ONNX 能 import 就行”，而是必须加载**当前 venv 与 sherpa wheel 匹配的 native runtime**。

启动路径会：

1. 优先项目 `.venv/Scripts`；
2. 使用 `os.add_dll_directory` 加入合法 DLL 目录；
3. 尝试 preload venv 中匹配的 `onnxruntime.dll`；
4. 拒绝静默使用 `C:\Windows\System32\onnxruntime.dll` 作为 fallback。

不要通过替换 System32 DLL 解决项目依赖问题。

如果出现 native DLL 问题，优先：

```bash
bash scripts/sync-all.sh
```

然后确认当前 Python、`.venv` 和 sherpa wheel/native core 是同一环境。

## 7. HTTP contracts

Media Runtime：

```text
GET  /health
POST /v1/asr
POST /v1/tts
GET  /v1/metrics/recent
```

ASR 接受 WAV contract；TTS 返回 WAV。

Character chat 仍通过：

```text
POST /v1/chat/messages
GET  /v1/events/stream
```

Voice 前端不绕过普通 PersonRuntime。

## 8. Voice interaction model

当前 Voice 是简化 call/dictation experience，不宣称 full duplex。

基本边界：

- browser-side simple VAD / recording；
- ASR → normal chat message；
- 等待 Character Runtime reaction；
- Character text → TTS；
- 播放期间避免机械重复提交 microphone turn。

已有 long-call scrolling / UI contract regression tests；后续优化 Voice UI 时不要回退聊天滚动与消息历史行为。

## 9. Resource policy

- 主 LLM / Vision 保持 cloud-only。
- ASR/TTS 可 CPU 运行；GPU 是 benchmark 证明有收益后的 accelerator，不是默认假设。
- ASR/TTS combined VRAM hard target：`<= 4 GB`；preferred `<= 3 GB`。
- 模型 lazy load 是合理策略；不要因为 Dev health probe 就强制加载全部本地模型。
- CI 不下载真实模型、不要求 GPU。

## 10. Benchmarks

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
- Media process VRAM

如果本地媒体已经很快而云 LLM wait 占主要时间，就停止继续微调 ASR/TTS，把优化精力放回真正瓶颈。

## 11. Test layers

### CI contract tests

不需要真实 model/GPU：

- WAV parsing/resampling
- fake ASR/TTS provider
- HTTP contracts
- lazy dependency boundary
- bootstrap/native runtime safety
- main server / media server separation
- frontend Voice contract

### Local real-model test

验证本机：

- cold/warm load
- inference latency
- language accuracy
- DLL/device
- CPU/GPU resource

### Dev Console live smoke

`:8002/dev` 可以执行真实：

```text
TTS -> generated WAV -> ASR
```

用于确认当前安装和真实模型链路，不是 mock contract。

## 12. Current non-goals

当前仍未把以下能力作为稳定 contract：

- WebRTC full duplex
- barge-in
- streaming ASR partials
- streaming TTS chunks
- voice cloning
- emotion TTS
- group call
- character-initiated call
- persistent raw audio

如果后续进入这些能力，应继续保持“Media 是渠道，Person 只有一个”的边界。
