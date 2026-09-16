# TTS Provider Runtime + Lab

`:9002` 当前既是本地 TTS Provider Runtime，也是多 Provider 试听/benchmark UI。

V1 接受这种合并实现，但需要明确两个不同职责：

```text
Provider Runtime
  -> 正式 Kokoro synthesis API

Lab UI
  -> Sherpa / Kokoro / CosyVoice audition
```

如果未来两者生命周期、资源或部署需求明显分离，再在大版本拆开；当前不为了架构美观制造额外进程。

## 1. Ports and scope

```text
Character Runtime         http://127.0.0.1:8000
Media Runtime             http://127.0.0.1:8001
Dev Console               http://127.0.0.1:8002/dev
Settings Center           http://127.0.0.1:8003/settings
TTS Provider Runtime+Lab  http://127.0.0.1:9002/tts
Optional CosyVoice        http://127.0.0.1:9012
```

`:9002` 当前暴露三类 provider：

1. **Sherpa VITS** — 通过 Media Runtime `:8001` 代理，用于试听比较；已保留本地 speaker `0 / 2 / 5`。
2. **Kokoro 82M v1.1 zh** — 运行在主 Python 3.12 环境；voice 为 `zf_001 / zf_002 / zf_003 / zf_004`。
3. **CosyVoice 300M SFT** — optional Python 3.10 sidecar，默认 `:9012`。

## 2. Formal chat TTS vs Lab audition

正式 Browser voice **始终调用稳定 Media Runtime contract**：

```text
POST :8001/v1/tts
```

正式 provider/voice 由 Settings Center / `config.yaml` 决定。

当前 V1 默认：

```yaml
tts_provider: "kokoro"
tts_voice: "zf_001"
tts_speed: 1.0
tts_device: "cpu"
```

因此默认正式路径是：

```text
Browser
  -> :8001/v1/tts
  -> :9002/v1/tts {provider=kokoro}
  -> Kokoro
```

如果配置为 Sherpa，则 `:8001` 直接使用本地 VITS。

**Lab 中切换 Provider/Voice 不会修改正式配置。** 要改变 Browser 默认 TTS，请在 `:8003/settings` 修改，并按 V1 restart policy 重启 stack。

## 3. Canonical setup

当前唯一推荐的模型准备入口：

```bash
bash scripts/setup-media-models.sh
```

它会：

1. 复用 `scripts/sync-all.sh` 同步 canonical `all` 环境；
2. 准备 SenseVoice ASR；
3. 准备 Sherpa VITS；
4. 运行 `scripts/prefetch_tts_models.py` 预下载 Kokoro model/voice。

`scripts/setup-tts-models.sh` 仍存在，但只是兼容 wrapper：

```text
setup-tts-models.sh -> setup-media-models.sh
```

新文档与日常操作不要再把 wrapper 当 canonical path。

准备完成后：

```bash
uv run character-stack --open tts
```

## 4. Kokoro current contract

模型仓库：

```text
hexgrad/Kokoro-82M-v1.1-zh
```

预下载资产：

```text
config.json
kokoro-v1_1-zh.pth
voices/zf_001.pt
voices/zf_002.pt
voices/zf_003.pt
voices/zf_004.pt
```

当前中文 voice：

```text
zf_001
zf_002
zf_003
zf_004
```

旧名称：

```text
zf_xiaobei
zf_xiaoni
zf_xiaoxiao
zf_xiaoyi
```

属于不同的基础 Kokoro voice inventory，不是当前 `v1.1-zh` 仓库里的 voice 文件，因此不要在当前配置中使用。

Kokoro request path 使用已经缓存的本地文件，不应该在第一次 synthesis 时临时下载 model/voice。

但是预下载不等于“第一次运行无成本”。首次请求仍可能发生：

- PyTorch weight deserialize；
- Chinese G2P init；
- CPU/GPU inference warmup。

默认 `tts_device: cpu`，因此首次 load/synthesis CPU 较高是正常现象。

要测试 CUDA，先验证当前 Torch build 真正支持 CUDA，再通过 Settings 或环境配置选择 device。不要仅因为机器有 NVIDIA GPU 就假设当前 Python 环境具备 CUDA Torch。

## 5. Sherpa

Sherpa VITS 仍有两个角色：

- 正式 TTS fallback；
- Lab 中的比较 provider。

Lab 通过 `:8001` 调用 Sherpa，不另外加载第二份 Sherpa runtime。

当前试听 voice：

```text
0
2
5
```

正式选择 Sherpa 时，`tts_voice` 应使用数字 speaker id。

## 6. CosyVoice sidecar

CosyVoice 当前不是主 stack 的强制组成，也没有完成一键环境自动化。

设计保持：

```text
Main project     Python 3.12  .venv
CosyVoice        Python 3.10  .venv-cosyvoice
```

已有入口：

```text
scripts/cosyvoice_sidecar.py
```

sidecar 默认：

```text
http://127.0.0.1:9012
```

当前**不存在** `scripts/setup-cosyvoice.sh`，不要在文档中声称已经自动化。

CosyVoice 不可用时：

- Lab provider 应显示 unavailable；
- 主 Character Stack 仍应可启动；
- 正式 Kokoro/Sherpa 路径不应因此失败。

如果试听和资源测量证明 CosyVoice 值得长期保留，再设计 one-click setup/start。

## 7. Current manual CosyVoice setup

当前仍可采用独立环境，例如：

```bash
mkdir -p .external
git clone --recursive https://github.com/QwenAudio/CosyVoice.git .external/CosyVoice
uv venv .venv-cosyvoice --python 3.10
uv pip install --python .venv-cosyvoice/Scripts/python.exe \
  -r .external/CosyVoice/requirements.txt \
  -i https://mirrors.aliyun.com/pypi/simple/
uv pip install --python .venv-cosyvoice/Scripts/python.exe huggingface_hub
```

模型与 upstream dependency 细节可能变化，因此这部分是本地实验步骤，不应被理解为主项目稳定安装 contract。

启动 sidecar：

```bash
COSYVOICE_ROOT="$(pwd)/.external/CosyVoice" \
COSYVOICE_MODEL_DIR="$(pwd)/.external/CosyVoice/pretrained_models/CosyVoice-300M-SFT" \
.venv-cosyvoice/Scripts/python.exe scripts/cosyvoice_sidecar.py
```

健康入口：

```text
http://127.0.0.1:9012/health
```

## 8. Audition workflow

使用同一段文本比较 Provider，重点观察：

- 中文基础音色；
- 普通话发音；
- 句尾语气；
- 标点/停顿；
- 长句稳定性；
- cold/warm latency；
- RAM/VRAM；
- RTF。

Lab 展示 provider、voice、inference latency、audio duration、sample rate、device 和 browser-observed HTTP duration。

多 Provider 批量生成应避免同时让大型模型争抢 RAM/VRAM；当前串行比较是合理 baseline。

## 9. Decision priority

当前产品优先级：

```text
自然好听的中文声音
>
稳定延迟 / 资源
>
不同人物不同 Voice
>
Emotion / Prosody
```

Zero-shot voice cloning 当前不是必要目标。

当前 benchmark 顺序可以保持：

```text
Sherpa VITS
Kokoro Mandarin
CosyVoice fixed-speaker SFT
```

但 benchmark 顺序不等于正式默认值；当前正式默认已经是 Kokoro `zf_001`。

## 10. Testing boundary

CI 可以验证：

- Provider API shape；
- formal `:8001 -> :9002` routing；
- missing-provider fallback/error boundary；
- package/import isolation；
- Settings contract。

CI 不能证明：

- 某个 voice “更好听”；
- 真实模型首次加载时间；
- Windows/GPU 实际性能；
- CosyVoice upstream environment 可安装。

这些必须通过本机 live audition/benchmark 验证。
