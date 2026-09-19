# GSV 每角色音色 + 正式接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 GSV-TTS 从"单参考、不可复现的 Lab 试听件"变成"每角色独立音色、输出稳定、接入正式 `:8001` 聊天链路"的语音源。

**Architecture:** sidecar 引入**参考注册表**（voice_id → 参考音频/文本 + 可选模型 override），共享一份底模，每角色只多一份参考 embedding；采样参数透传到 `infer_batched` 并加时长保护；`media_server` 复用既有的 kokoro/edge Lab 路径接入 gsv，并补上未知 provider 守卫。

**Tech Stack:** Python 3.12 / FastAPI / pydantic / numpy / pytest；sidecar 运行在 `.external/GSV-TTS-Lite/.venv`（torch 2.9.1+cu128），**主环境不引入 torch**。

**Spec:** `docs/superpowers/specs/2026-09-19-gsv-formal-integration-design.md`

## Global Constraints

- **主环境不得引入 torch 或 gsv_tts。** 所有 GSV 相关重依赖只在 sidecar（`.external/GSV-TTS-Lite/.venv`）内。测试一律通过 `tts_factory` 注入 fake，不得需要 CUDA。
- **不要修改** `media_server.py:174` 的 qwen3 分支 —— GSV 走 Lab 路径。
- **不要**往 `settings_store.py:76-98` 的扁平 `tts_voice` 列表里加 gsv 音色。
- 时长/采样默认值以 Phase 0/1 实测为准；本 plan 给出的初值是待验证的起点。
- 每个 Task 结束必须 `git commit`。
- 提交信息结尾：`Co-Authored-By: Claude Code <noreply@anthropic.com>`
- 测试命令统一用 `uv run pytest`。
- **不要**依赖读源码断言字符串的测试（`tests/test_settings_center.py:146-167`、`tests/test_dev_stack.py:4-27`）作为正确性证据；新测试一律行为导向。

## 基线事实（已实测，不必重新验证）

- `main` @ `7f22f8a`；4 个相关测试文件 `32 passed`。
- sidecar `:9014`，Lab `:9002`。加载 `load_ms≈34638`，CUDA allocated `1445 MB`，`sample_rate=32000`。
- 延迟：首次请求 `4131ms`（RTF 1.17）；短句稳态 `477–910ms`；28 字句稳态 `930–1658ms`。
- **缺陷 1**：`GsvTtsRequest` 无采样参数 → 同文本三次产出 `6.00s / 10.72s / 6.68s`。
- **缺陷 2**：`media_server` `/v1/tts` 无 `else` → 未知 provider 静默降级为 Sherpa 且返回 200。
- **缺陷 3**：`dev_stack.py:71` 白名单缺 `edge`（既有遗漏）。
- 模型资产在**另一个仓库**：`C:/Users/cute/projects/Shinsekai/data/models/deepseek1/`（`Murasame-e15.ckpt` / `Murasame_e8_s192.pth` / `MAY_0035.wav`）。共享底模在 `~/.cache/gsv/`（`s1v3.ckpt` / `s2Gv2ProPlus.pth`）。
- 引擎 `infer_batched` 已有但 sidecar 未暴露的参数：`top_k=15, top_p=1.0, temperature=1.0, repetition_penalty=1.35, noise_scale=0.5`。**引擎没有 `seed` 参数。**

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/character_memory/gsv_voices.py` | 参考注册表：数据结构、加载、校验、默认值。纯逻辑，不 import torch | **新建** |
| `src/character_memory/gsv_tts_experiment.py` | sidecar：运行时 + HTTP 契约 | 修改 |
| `src/character_memory/media_server.py` | 正式路由 + 守卫 | 修改 |
| `src/character_memory/config.py` | `tts_provider` 取值 | 修改 |
| `src/character_memory/dev_stack.py` | 白名单 + 启动 sidecar | 修改 |
| `src/character_memory/settings_store.py` | Settings Center provider 选项 | 修改 |
| `scripts/start-gsv-tts.sh` | 启动脚本，注入注册表 | 修改 |
| `tests/test_gsv_voices.py` | 注册表单测 | **新建** |
| `tests/test_gsv_tts_experiment.py` | sidecar 契约测试 | 修改 |
| `tests/test_media_runtime.py` | 正式路由测试 | 修改 |
| `tests/test_dev_stack.py` | 白名单测试 | 修改 |

**为什么注册表单独成文件**：加载与校验是纯数据逻辑，不需要 torch/CUDA 就能测；塞进 `gsv_tts_experiment.py`（已 436 行）会让该文件继续膨胀且测试被迫拉起 sidecar 依赖。

---

## Phase 0：Spike（决策门）

Phase 0 不写产品代码，只产出**结论**。两个 spike 的结论分别决定 Phase 1 与 Phase 4 的形态。结论写进 `docs/superpowers/notes/2026-09-19-gsv-spike-results.md`（新建）。

### Task 0.1: 确定性 spike —— `torch.manual_seed` 能否让 GSV 可复现

**Files:**
- Create: `docs/superpowers/notes/2026-09-19-gsv-spike-results.md`
- Create: `.pytest-tmp/gsv-spike/seed_probe.py`（临时脚本，不提交）

**Interfaces:**
- Produces: 结论「确定性可达 / 不可达」。Phase 1 Task 1.3 依赖此结论决定是否暴露 `seed` 字段。

- [ ] **Step 1: 确认 sidecar 在跑**

```bash
curl -sS --max-time 5 http://127.0.0.1:9014/health
```

Expected: `"ready":true,"loaded":true`

若未运行，按 spec §3.1 用 `bash scripts/start-gsv-tts.sh` 启动，并按 §基线事实设置 `GSV_TTS_GPT_MODEL` / `GSV_TTS_SOVITS_MODEL` / `GSV_TTS_REF_AUDIO` / `GSV_TTS_REF_TEXT` / `GSV_TTS_VOICE`。

- [ ] **Step 2: 写探针脚本，在同一 seed 下连跑 3 次**

`.pytest-tmp/gsv-spike/seed_probe.py`：

```python
"""Probe whether torch.manual_seed makes GSV inference reproducible."""
import hashlib
import sys

sys.path.insert(0, r"C:/Users/cute/projects/character_memory/.external/GSV-TTS-Lite")
sys.path.insert(0, r"C:/Users/cute/projects/character_memory/src")

import torch
from character_memory.gsv_tts_experiment import GsvTtsRuntime, GsvTtsRequest

ASSETS = r"C:/Users/cute/projects/Shinsekai/data/models/deepseek1/"
TEXT = "嗯，我知道了，我们继续。"

runtime = GsvTtsRuntime(
    gpt_model=ASSETS + "Murasame-e15.ckpt",
    sovits_model=ASSETS + "Murasame_e8_s192.pth",
    ref_audio=ASSETS + "MAY_0035.wav",
    ref_text="ねえねえ、何があったの？ 顔色、すごく悪いけど。",
    device="cuda",
    default_voice="murasame",
    preload=True,
)

for i in range(3):
    torch.manual_seed(1234)
    torch.cuda.manual_seed_all(1234)
    result = runtime.synthesize(GsvTtsRequest(text=TEXT, voice="murasame", language="zh", speed=1.0))
    digest = hashlib.sha256(result.audio).hexdigest()[:16]
    print(f"run {i}: bytes={len(result.audio)} audio_ms={result.audio_ms} sha256={digest}", flush=True)
```

- [ ] **Step 3: 运行探针**

```bash
.external/GSV-TTS-Lite/.venv/Scripts/python.exe .pytest-tmp/gsv-spike/seed_probe.py
```

Expected（若确定性可达）：三次 `sha256` 完全相同。
Expected（若不可达）：`sha256` 不同，且 `audio_ms` 有漂移。

- [ ] **Step 4: 记录结论**

在 `docs/superpowers/notes/2026-09-19-gsv-spike-results.md` 写入：探针输出原文、`sha256` 是否一致、`audio_ms` 极差、以及明确结论「确定性可达」或「确定性不可达」。

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/notes/2026-09-19-gsv-spike-results.md
git commit -m "Record GSV determinism spike result

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 0.2: 合成参考 spike —— Qwen3 生成的参考能否用于 GSV 克隆

**Files:**
- Modify: `docs/superpowers/notes/2026-09-19-gsv-spike-results.md`

**Interfaces:**
- Produces: 结论「合成参考可用 / 不可用」。决定 Phase 4 是否按 Qwen3 合成方案实现，或改为人工素材。

- [ ] **Step 1: 起 Qwen3 sidecar**

```bash
bash scripts/start-qwen3-tts.sh   # 若脚本名不同，用 dev_stack 中 qwen3 分支的实际启动方式
curl -sS --max-time 5 http://127.0.0.1:9013/health
```

Expected: `ready` 为 true。若 Qwen3 不可用，跳过本 Task 并在笔记中记「阻塞：Qwen3 不可用，Phase 4 待定」。

- [ ] **Step 2: 用 Qwen3 合成一段参考音频**

参考文本固定为 8–20 字、语义中性、无生僻字，例如：

```
今天天气不错，我们出去走走吧。
```

保存为 `.pytest-tmp/gsv-spike/qwen3_ref.wav`，并记下**确切文本**（后续 `ref_text` 必须逐字一致）。

- [ ] **Step 3: 用该参考重启 GSV，A/B 对比**

停止当前 GSV，用 `GSV_TTS_REF_AUDIO=.pytest-tmp/gsv-spike/qwen3_ref.wav`、`GSV_TTS_REF_TEXT=<上一步文本>` 重启，合成同一句话，存为 `.pytest-tmp/gsv-spike/out_synth_ref.wav`。

再用 `MAY_0035.wav` 参考合成同一句话，存为 `.pytest-tmp/gsv-spike/out_human_ref.wav`。

- [ ] **Step 4: 人工试听并记录**

**这是主观判断，必须由人来做。** 对比两个文件，记录：音色相似度、是否有金属味/发闷、是否有异常停顿或拖长。给出「可用 / 勉强 / 不可用」结论。

- [ ] **Step 5: 记录结论并 Commit**

写入同一笔记文件。若结论为「不可用」，明确写出 Phase 4 改为人工素材方案。

```bash
git add docs/superpowers/notes/2026-09-19-gsv-spike-results.md
git commit -m "Record GSV synthetic-reference spike result

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Phase 1：稳定性

Phase 1 结束时，跑飞的输出不再进入链路，且确定性结论有实测依据。

### Task 1.1: 采样参数透传

**Files:**
- Modify: `src/character_memory/gsv_tts_experiment.py`（`GsvTtsRequest` 在 :39-43，`synthesize` 的 `infer_batched` 调用在 :276-287）
- Test: `tests/test_gsv_tts_experiment.py`

**Interfaces:**
- Consumes: 无
- Produces: `GsvTtsRequest` 新增字段 `temperature: float`、`top_k: int`、`top_p: float`、`repetition_penalty: float`、`noise_scale: float`，均有默认值。Task 1.3 / Phase 4 沿用这些字段名。

- [ ] **Step 1: 写失败测试**

在 `tests/test_gsv_tts_experiment.py` 的 `FakeTts` 类中，`infer_batched` 已经用 `kwargs` 记录调用（:119-120），无需改动。新增测试：

```python
def test_gsv_sampling_params_reach_infer_batched(tmp_path):
    gpt = tmp_path / "v.ckpt"
    sovits = tmp_path / "v.pth"
    ref = tmp_path / "r.wav"
    for path in (gpt, sovits, ref):
        path.write_bytes(b"x")

    calls = {}

    class FakeTts:
        def __init__(self, **kwargs):
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, path):
            pass

        def load_sovits_model(self, path):
            pass

        def cache_spk_audio(self, path, **kwargs):
            pass

        def cache_prompt_audio(self, **kwargs):
            pass

        def infer_batched(self, **kwargs):
            calls["infer"] = kwargs
            return (SimpleNamespace(audio_data=np.zeros(8, dtype=np.float32), samplerate=32000),)

    runtime = GsvTtsRuntime(
        gpt_model=str(gpt),
        sovits_model=str(sovits),
        ref_audio=str(ref),
        ref_text="参考。",
        device="cpu",
        default_voice="murasame",
        tts_factory=FakeTts,
    )
    runtime.synthesize(
        GsvTtsRequest(
            text="你好",
            voice="murasame",
            temperature=0.7,
            top_k=20,
            top_p=0.9,
            repetition_penalty=1.5,
            noise_scale=0.4,
        )
    )

    assert calls["infer"]["temperature"] == 0.7
    assert calls["infer"]["top_k"] == 20
    assert calls["infer"]["top_p"] == 0.9
    assert calls["infer"]["repetition_penalty"] == 1.5
    assert calls["infer"]["noise_scale"] == 0.4
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_gsv_tts_experiment.py::test_gsv_sampling_params_reach_infer_batched -v
```

Expected: FAIL — `TypeError` 或 `ValidationError`，因为 `GsvTtsRequest` 不接受这些字段。

- [ ] **Step 3: 实现**

在 `gsv_tts_experiment.py` 的常量区（:19-21 附近）新增：

```python
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TOP_K = 15
DEFAULT_TOP_P = 0.9
DEFAULT_REPETITION_PENALTY = 1.5
DEFAULT_NOISE_SCALE = 0.5
```

替换 `GsvTtsRequest`（:39-43）：

```python
class GsvTtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = Field(default=DEFAULT_VOICE, max_length=128)
    language: str = Field(default="zh", max_length=16)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    temperature: float = Field(default=DEFAULT_TEMPERATURE, ge=0.1, le=2.0)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=0, le=100)
    top_p: float = Field(default=DEFAULT_TOP_P, gt=0.0, le=1.0)
    repetition_penalty: float = Field(default=DEFAULT_REPETITION_PENALTY, ge=1.0, le=3.0)
    noise_scale: float = Field(default=DEFAULT_NOISE_SCALE, ge=0.0, le=1.5)
```

在 `synthesize` 的 `infer_batched` 调用（:276-287）中，`speed=float(request.speed),` 之后加入：

```python
                    temperature=float(request.temperature),
                    top_k=int(request.top_k),
                    top_p=float(request.top_p),
                    repetition_penalty=float(request.repetition_penalty),
                    noise_scale=float(request.noise_scale),
```

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_gsv_tts_experiment.py -v
```

Expected: 全部 PASS（含既有 3 个测试）。

- [ ] **Step 5: Commit**

```bash
git add src/character_memory/gsv_tts_experiment.py tests/test_gsv_tts_experiment.py
git commit -m "Expose GSV sampling parameters

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 1.2: 时长保护

**Files:**
- Modify: `src/character_memory/gsv_tts_experiment.py`（`synthesize` :292-317）
- Test: `tests/test_gsv_tts_experiment.py`

**Interfaces:**
- Consumes: Task 1.1 的 `GsvTtsRequest`
- Produces: `synthesize` 在音频超过 `max_audio_ms` 时截断。新增模块常量 `DEFAULT_MAX_AUDIO_MS`。响应头新增 `X-TTS-Truncated`（`"true"`/`"false"`）。

- [ ] **Step 1: 写失败测试**

```python
def test_gsv_truncates_runaway_audio(tmp_path):
    gpt = tmp_path / "v.ckpt"
    sovits = tmp_path / "v.pth"
    ref = tmp_path / "r.wav"
    for path in (gpt, sovits, ref):
        path.write_bytes(b"x")

    # 20 秒 @ 32000Hz 的"跑飞"输出
    huge = np.zeros(32000 * 20, dtype=np.float32)

    class FakeTts:
        def __init__(self, **kwargs):
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, path):
            pass

        def load_sovits_model(self, path):
            pass

        def cache_spk_audio(self, path, **kwargs):
            pass

        def cache_prompt_audio(self, **kwargs):
            pass

        def infer_batched(self, **kwargs):
            return (SimpleNamespace(audio_data=huge, samplerate=32000),)

    runtime = GsvTtsRuntime(
        gpt_model=str(gpt),
        sovits_model=str(sovits),
        ref_audio=str(ref),
        ref_text="参考。",
        device="cpu",
        default_voice="murasame",
        tts_factory=FakeTts,
        max_audio_ms=8000.0,
    )
    result = runtime.synthesize(GsvTtsRequest(text="你好", voice="murasame"))

    assert result.truncated is True
    assert result.audio_ms <= 8000.0
    with wave.open(io.BytesIO(result.audio), "rb") as wav:
        assert wav.getnframes() == 32000 * 8
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_gsv_tts_experiment.py::test_gsv_truncates_runaway_audio -v
```

Expected: FAIL — `GsvTtsRuntime.__init__` 不接受 `max_audio_ms`。

- [ ] **Step 3: 实现**

常量区新增：

```python
DEFAULT_MAX_AUDIO_MS = 15000.0
```

`GsvTtsResult`（:24-37）新增字段：

```python
    truncated: bool = False
```

`GsvTtsRuntime.__init__` 参数表（:67-81）新增 `max_audio_ms: float = DEFAULT_MAX_AUDIO_MS,`，并在函数体（:91 附近）加：

```python
        self.max_audio_ms = float(max_audio_ms)
```

`status()` 返回值（:198-215）新增：

```python
            "max_audio_ms": self.max_audio_ms,
```

`synthesize` 中，把 :298-301 的采样处理替换为带截断的版本：

```python
            samples = np.asarray(raw_audio, dtype=np.float32).reshape(-1)
            if not samples.size:
                raise RuntimeError("GSV-TTS-Lite returned empty audio")
            sample_rate = int(getattr(clip, "samplerate", DEFAULT_SAMPLE_RATE) or DEFAULT_SAMPLE_RATE)
            max_frames = int(self.max_audio_ms * sample_rate / 1000.0)
            truncated = samples.size > max_frames
            if truncated:
                samples = samples[:max_frames]
```

并把 `return GsvTtsResult(...)` 中的 `audio_ms=` 计算保持为 `len(samples) * 1000.0 / sample_rate`（已基于截断后的 samples），新增 `truncated=truncated,`。

在 `create_gsv_tts_app` 的响应头（:372-384）加入：

```python
                "X-TTS-Truncated": "true" if result.truncated else "false",
```

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_gsv_tts_experiment.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/character_memory/gsv_tts_experiment.py tests/test_gsv_tts_experiment.py
git commit -m "Guard GSV against runaway audio length

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 1.3: 采样默认值定标（依赖 Task 0.1 结论）

**Files:**
- Modify: `src/character_memory/gsv_tts_experiment.py`（常量区）
- Modify: `docs/superpowers/notes/2026-09-19-gsv-spike-results.md`

**Interfaces:**
- Consumes: Task 0.1 的确定性结论，Task 1.1/1.2 的字段与常量
- Produces: 定标后的默认值。**若 Task 0.1 结论为「确定性可达」**，额外产出 `seed: int | None` 字段。

- [ ] **Step 1: 若 Task 0.1 结论为「确定性可达」，加 seed 字段**

先写失败测试：

```python
def test_gsv_seed_reaches_infer_batched_when_set(tmp_path):
    """seed 仅在 Phase 0 验证确定性可达时才有意义。"""
    # 复用 Task 1.1 的 FakeTts 骨架，断言 calls["infer"]["seed"] == 1234
```

再在 `synthesize` 中调用 `infer_batched` 前插入：

```python
            seed = request.seed
            if seed is not None:
                if self._torch is not None:
                    self._torch.manual_seed(int(seed))
                    if self._device().startswith("cuda"):
                        self._torch.cuda.manual_seed_all(int(seed))
```

并在 `GsvTtsRequest` 加 `seed: int | None = Field(default=None, ge=0, le=2**31 - 1)`。

> **若 Task 0.1 结论为「确定性不可达」，跳过本 Step，改为在笔记中写明"放弃可复现承诺"。**

- [ ] **Step 2: 用真实 sidecar 定标**

对同一句话用若干组参数各合成 5 次，记录 `audio_ms` 极差与人工听感：

| temperature | top_k | top_p | repetition_penalty |
|---|---|---|---|
| 1.0 (现状) | 15 | 1.0 | 1.35 |
| 0.7 | 15 | 0.9 | 1.5 |
| 0.5 | 10 | 0.85 | 1.7 |

选**极差最小且听感自然**的一组作为默认值。

- [ ] **Step 3: 把选定值写回常量**

```bash
uv run pytest tests/test_gsv_tts_experiment.py -v
```

Expected: 全部 PASS（若改了默认值，Task 1.1 的显式传值测试不受影响）。

- [ ] **Step 4: 记录定标数据并 Commit**

```bash
git add src/character_memory/gsv_tts_experiment.py tests/test_gsv_tts_experiment.py docs/superpowers/notes/2026-09-19-gsv-spike-results.md
git commit -m "Calibrate GSV sampling defaults from measurements

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Phase 2：音色层（参考注册表）

Phase 2 结束时，同一 sidecar 进程内多个 voice 交替请求各自音色正确。

**背景约束（已核对 `gsv_tts/TTS.py`）**：`load_gpt_model(*paths)` / `load_sovits_model(*paths)` 支持一次加载多个模型并存进 `self.gpt_models` / `self.sovits_models` 字典；`cache_spk_audio(*paths, sovits_model=None)` 与 `cache_prompt_audio(paths, texts, prompt_language)` 的缓存**按路径为键**，因此多个 voice 的参考可以共存于缓存。`infer_batched(..., gpt_model=, sovits_model=)` 支持按调用指定模型。**注册表方案在引擎层面可行。**

### Task 2.1: 注册表模块

**Files:**
- Create: `src/character_memory/gsv_voices.py`
- Test: `tests/test_gsv_voices.py`

**Interfaces:**
- Consumes: 无（纯数据逻辑，不 import torch / gsv_tts）
- Produces:
  - `@dataclass(frozen=True) GsvVoice(voice_id: str, ref_audio: str, ref_text: str, gpt_model: str | None, sovits_model: str | None)`
  - `build_voice_registry(raw_json: str, *, default_gpt: str, default_sovits: str, legacy_voice: str, legacy_ref_audio: str, legacy_ref_text: str) -> dict[str, GsvVoice]`
  - 异常 `GsvVoiceConfigError(ValueError)`

- [ ] **Step 1: 写失败测试**

`tests/test_gsv_voices.py`：

```python
from __future__ import annotations

import pytest

from character_memory.gsv_voices import GsvVoiceConfigError, build_voice_registry


def _assets(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"wav")
    return ref


def test_legacy_env_builds_single_entry_registry(tmp_path):
    ref = _assets(tmp_path)
    registry = build_voice_registry(
        "",
        default_gpt="base.ckpt",
        default_sovits="base.pth",
        legacy_voice="murasame",
        legacy_ref_audio=str(ref),
        legacy_ref_text="参考。",
    )
    assert list(registry) == ["murasame"]
    voice = registry["murasame"]
    assert voice.ref_audio == str(ref)
    assert voice.ref_text == "参考。"
    assert voice.gpt_model == "base.ckpt"
    assert voice.sovits_model == "base.pth"


def test_entry_without_model_override_inherits_shared_base(tmp_path):
    ref = _assets(tmp_path)
    registry = build_voice_registry(
        '{"haru": {"ref_audio": "%s", "ref_text": "你好。"}}' % ref,
        default_gpt="base.ckpt",
        default_sovits="base.pth",
        legacy_voice="murasame",
        legacy_ref_audio="",
        legacy_ref_text="",
    )
    assert registry["haru"].gpt_model == "base.ckpt"
    assert registry["haru"].sovits_model == "base.pth"


def test_entry_with_model_override_keeps_its_own_models(tmp_path):
    ref = _assets(tmp_path)
    registry = build_voice_registry(
        '{"murasame": {"ref_audio": "%s", "ref_text": "x", '
        '"gpt_model": "ft.ckpt", "sovits_model": "ft.pth"}}' % ref,
        default_gpt="base.ckpt",
        default_sovits="base.pth",
        legacy_voice="legacy",
        legacy_ref_audio="",
        legacy_ref_text="",
    )
    assert registry["murasame"].gpt_model == "ft.ckpt"
    assert registry["murasame"].sovits_model == "ft.pth"


def test_missing_ref_audio_file_is_rejected(tmp_path):
    with pytest.raises(GsvVoiceConfigError, match="not found"):
        build_voice_registry(
            '{"haru": {"ref_audio": "%s", "ref_text": "x"}}' % (tmp_path / "nope.wav"),
            default_gpt="b.ckpt",
            default_sovits="b.pth",
            legacy_voice="l",
            legacy_ref_audio="",
            legacy_ref_text="",
        )


def test_empty_ref_text_is_rejected(tmp_path):
    ref = _assets(tmp_path)
    with pytest.raises(GsvVoiceConfigError, match="ref_text"):
        build_voice_registry(
            '{"haru": {"ref_audio": "%s", "ref_text": "  "}}' % ref,
            default_gpt="b.ckpt",
            default_sovits="b.pth",
            legacy_voice="l",
            legacy_ref_audio="",
            legacy_ref_text="",
        )


def test_malformed_json_is_rejected():
    with pytest.raises(GsvVoiceConfigError, match="JSON"):
        build_voice_registry(
            "{not json",
            default_gpt="b.ckpt",
            default_sovits="b.pth",
            legacy_voice="l",
            legacy_ref_audio="",
            legacy_ref_text="",
        )


def test_legacy_voice_absent_when_ref_audio_missing():
    """没有注册表也没有 legacy 资产时返回空表，由 status() 报 not ready。"""
    registry = build_voice_registry(
        "",
        default_gpt="b.ckpt",
        default_sovits="b.pth",
        legacy_voice="murasame",
        legacy_ref_audio="",
        legacy_ref_text="",
    )
    assert registry == {}
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_gsv_voices.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'character_memory.gsv_voices'`

- [ ] **Step 3: 实现**

`src/character_memory/gsv_voices.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


class GsvVoiceConfigError(ValueError):
    """Raised when the GSV voice registry cannot be built."""


@dataclass(frozen=True)
class GsvVoice:
    voice_id: str
    ref_audio: str
    ref_text: str
    gpt_model: str
    sovits_model: str


def build_voice_registry(
    raw_json: str,
    *,
    default_gpt: str,
    default_sovits: str,
    legacy_voice: str,
    legacy_ref_audio: str,
    legacy_ref_text: str,
) -> dict[str, GsvVoice]:
    """Build the voice_id -> GsvVoice map.

    With no raw_json, falls back to the single legacy env-configured voice so
    existing deployments keep working unchanged. Entries that omit gpt_model /
    sovits_model inherit the shared base models.
    """
    gpt_default = str(default_gpt or "").strip()
    sovits_default = str(default_sovits or "").strip()
    entries: dict[str, dict] = {}

    raw = str(raw_json or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GsvVoiceConfigError(f"GSV voice registry is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise GsvVoiceConfigError("GSV voice registry must be a JSON object")
        entries = parsed
    elif str(legacy_ref_audio or "").strip():
        entries = {
            str(legacy_voice or "gsv-default").strip() or "gsv-default": {
                "ref_audio": legacy_ref_audio,
                "ref_text": legacy_ref_text,
            }
        }

    registry: dict[str, GsvVoice] = {}
    for voice_id, spec in entries.items():
        name = str(voice_id or "").strip()
        if not name:
            raise GsvVoiceConfigError("GSV voice registry has an entry with an empty id")
        if not isinstance(spec, dict):
            raise GsvVoiceConfigError(f"GSV voice '{name}' must be a JSON object")

        ref_audio = str(spec.get("ref_audio") or "").strip()
        ref_text = str(spec.get("ref_text") or "").strip()
        if not ref_audio:
            raise GsvVoiceConfigError(f"GSV voice '{name}' is missing ref_audio")
        if not Path(ref_audio).is_file():
            raise GsvVoiceConfigError(f"GSV voice '{name}' ref_audio not found: {ref_audio}")
        if not ref_text:
            raise GsvVoiceConfigError(f"GSV voice '{name}' is missing ref_text")

        registry[name] = GsvVoice(
            voice_id=name,
            ref_audio=ref_audio,
            ref_text=ref_text,
            gpt_model=str(spec.get("gpt_model") or "").strip() or gpt_default,
            sovits_model=str(spec.get("sovits_model") or "").strip() or sovits_default,
        )
    return registry
```

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_gsv_voices.py -v
```

Expected: 7 passed。

- [ ] **Step 5: Commit**

```bash
git add src/character_memory/gsv_voices.py tests/test_gsv_voices.py
git commit -m "Add GSV voice registry with shared-base fallback

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 2.2: 把注册表接进 runtime

**Files:**
- Modify: `src/character_memory/gsv_tts_experiment.py`（`__init__` :67-97、`_asset_status` :119-134、`load` :217-261、`status` :193-215、`create_gsv_tts_app` :327-338、`main` :390-426）
- Test: `tests/test_gsv_tts_experiment.py`

**Interfaces:**
- Consumes: Task 2.1 的 `build_voice_registry` / `GsvVoice` / `GsvVoiceConfigError`
- Produces: `GsvTtsRuntime(..., voices: dict[str, GsvVoice] | None = None, ...)`；`runtime.voices` 属性；`status()["voices"]` 返回全部 voice id。新增 env：`GSV_TTS_VOICES`（JSON）、`GSV_TTS_BASE_GPT_MODEL`、`GSV_TTS_BASE_SOVITS_MODEL`。

- [ ] **Step 1: 写失败测试**

```python
def test_runtime_lists_all_registry_voices(tmp_path):
    base_gpt = tmp_path / "base.ckpt"
    base_sovits = tmp_path / "base.pth"
    ref_a = tmp_path / "a.wav"
    ref_b = tmp_path / "b.wav"
    for path in (base_gpt, base_sovits, ref_a, ref_b):
        path.write_bytes(b"x")

    registry = build_voice_registry(
        '{"haru": {"ref_audio": "%s", "ref_text": "a"}, '
        '"momo": {"ref_audio": "%s", "ref_text": "b"}}' % (ref_a, ref_b),
        default_gpt=str(base_gpt),
        default_sovits=str(base_sovits),
        legacy_voice="",
        legacy_ref_audio="",
        legacy_ref_text="",
    )
    runtime = GsvTtsRuntime(
        gpt_model=str(base_gpt),
        sovits_model=str(base_sovits),
        ref_audio="",
        ref_text="",
        device="cpu",
        default_voice="haru",
        voices=registry,
        tts_factory=lambda **kwargs: None,
    )
    status = runtime.status()
    assert sorted(status["voices"]) == ["haru", "momo"]
    assert status["default_voice"] == "haru"


def test_load_caches_every_registry_voice(tmp_path):
    base_gpt = tmp_path / "base.ckpt"
    base_sovits = tmp_path / "base.pth"
    ref_a = tmp_path / "a.wav"
    ref_b = tmp_path / "b.wav"
    for path in (base_gpt, base_sovits, ref_a, ref_b):
        path.write_bytes(b"x")

    registry = build_voice_registry(
        '{"haru": {"ref_audio": "%s", "ref_text": "a"}, '
        '"momo": {"ref_audio": "%s", "ref_text": "b"}}' % (ref_a, ref_b),
        default_gpt=str(base_gpt),
        default_sovits=str(base_sovits),
        legacy_voice="",
        legacy_ref_audio="",
        legacy_ref_text="",
    )

    calls = {"gpt": [], "sovits": [], "cache_spk": [], "cache_prompt": []}

    class FakeTts:
        def __init__(self, **kwargs):
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, *paths):
            calls["gpt"].extend(paths)

        def load_sovits_model(self, *paths):
            calls["sovits"].extend(paths)

        def cache_spk_audio(self, *paths, **kwargs):
            calls["cache_spk"].extend(paths)

        def cache_prompt_audio(self, **kwargs):
            calls["cache_prompt"].append(kwargs)

    runtime = GsvTtsRuntime(
        gpt_model=str(base_gpt),
        sovits_model=str(base_sovits),
        ref_audio="",
        ref_text="",
        device="cpu",
        default_voice="haru",
        voices=registry,
        tts_factory=FakeTts,
    )
    runtime.load()

    # 共享底模只加载一次
    assert calls["gpt"] == [str(base_gpt)]
    assert calls["sovits"] == [str(base_sovits)]
    # 两个 voice 的参考都被缓存
    assert sorted(calls["cache_spk"]) == sorted([str(ref_a), str(ref_b)])
    texts = sorted(c["prompt_audio_texts"] for c in calls["cache_prompt"])
    assert texts == ["a", "b"]
```

> 测试文件顶部需新增 import：`from character_memory.gsv_voices import build_voice_registry`。

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_gsv_tts_experiment.py -k registry -v
```

Expected: FAIL — `__init__` 不接受 `voices`

- [ ] **Step 3: 实现**

`gsv_tts_experiment.py` 顶部 import 区加：

```python
from character_memory.gsv_voices import GsvVoice, GsvVoiceConfigError, build_voice_registry
```

`__init__` 参数表加 `voices: dict[str, GsvVoice] | None = None,`，函数体中，在 `self.default_voice = ...`（:88）之后插入：

```python
        self.voices: dict[str, GsvVoice] = dict(voices or {})
        if self.voices and self.default_voice not in self.voices:
            self.default_voice = next(iter(self.voices))
        self._cached_voices: set[str] = set()
```

`_asset_status`（:119-134）替换为：

```python
    def _asset_status(self) -> tuple[bool, str | None]:
        if self.voices:
            pending = [v.voice_id for v in self.voices.values() if not Path(v.ref_audio).is_file()]
            if pending:
                return False, f"GSV voice reference not found: {', '.join(pending)}"
            if not any(v.gpt_model for v in self.voices.values()):
                return False, "No GSV GPT model configured"
            if not any(v.sovits_model for v in self.voices.values()):
                return False, "No GSV SoVITS model configured"
            return True, None

        required = {
            "GSV_TTS_GPT_MODEL": self.gpt_model,
            "GSV_TTS_SOVITS_MODEL": self.sovits_model,
            "GSV_TTS_REF_AUDIO": self.ref_audio,
        }
        missing_config = [name for name, value in required.items() if not value]
        if not self.ref_text:
            missing_config.append("GSV_TTS_REF_TEXT")
        if missing_config:
            return False, f"Missing GSV configuration: {', '.join(missing_config)}"

        missing_files = [value for value in required.values() if value and not Path(value).is_file()]
        if missing_files:
            return False, f"GSV asset not found: {', '.join(missing_files)}"
        return True, None
```

`model_label`（:99-103）替换为：

```python
    @property
    def model_label(self) -> str:
        if self.voices:
            models = {v.sovits_model for v in self.voices.values() if v.sovits_model}
            base = Path(next(iter(sorted(models)))).name if models else "unset-sovits"
            if len(models) > 1:
                return f"{len(models)} model pairs ({len(self.voices)} voices)"
            return f"{base} ({len(self.voices)} voices)"
        gpt = Path(self.gpt_model).name if self.gpt_model else "unset-gpt"
        sovits = Path(self.sovits_model).name if self.sovits_model else "unset-sovits"
        return f"{gpt}+{sovits}"
```

`status()` 的 `"voices": [self.default_voice],`（:205）替换为：

```python
            "voices": sorted(self.voices) if self.voices else [self.default_voice],
            "voice_details": {
                v.voice_id: {"ref_audio": v.ref_audio, "cached": v.voice_id in self._cached_voices}
                for v in self.voices.values()
            } if self.voices else {},
```

`load()` 中，把 :241-251 的模型加载与缓存替换为：

```python
                if self.models_dir:
                    kwargs["models_dir"] = self.models_dir
                engine = factory(**kwargs)

                if self.voices:
                    gpt_paths = sorted({v.gpt_model for v in self.voices.values() if v.gpt_model})
                    sovits_paths = sorted({v.sovits_model for v in self.voices.values() if v.sovits_model})
                    engine.load_gpt_model(*gpt_paths)
                    engine.load_sovits_model(*sovits_paths)
                    for voice in self.voices.values():
                        engine.cache_spk_audio(voice.ref_audio, sovits_model=voice.sovits_model)
                        engine.cache_prompt_audio(
                            prompt_audio_paths=voice.ref_audio,
                            prompt_audio_texts=voice.ref_text,
                            prompt_language=self.prompt_language,
                        )
                        self._cached_voices.add(voice.voice_id)
                else:
                    engine.load_gpt_model(self.gpt_model)
                    engine.load_sovits_model(self.sovits_model)
                    engine.cache_spk_audio(self.ref_audio, sovits_model=self.sovits_model)
                    engine.cache_prompt_audio(
                        prompt_audio_paths=self.ref_audio,
                        prompt_audio_texts=self.ref_text,
                        prompt_language=self.prompt_language,
                    )
```

`create_gsv_tts_app` 的 runtime 构造（:327-338）中，`preload=...` 之前加入：

```python
        voices=build_voice_registry(
            os.getenv("GSV_TTS_VOICES", ""),
            default_gpt=os.getenv("GSV_TTS_BASE_GPT_MODEL", "") or os.getenv("GSV_TTS_GPT_MODEL", ""),
            default_sovits=os.getenv("GSV_TTS_BASE_SOVITS_MODEL", "") or os.getenv("GSV_TTS_SOVITS_MODEL", ""),
            legacy_voice=os.getenv("GSV_TTS_VOICE", DEFAULT_VOICE),
            legacy_ref_audio=os.getenv("GSV_TTS_REF_AUDIO", ""),
            legacy_ref_text=os.getenv("GSV_TTS_REF_TEXT", ""),
        ),
```

`main()` 的 `runtime = GsvTtsRuntime(...)`（:415-426）同样加入 `voices=` 参数（与上面相同的表达式），并新增 CLI 参数：

```python
    parser.add_argument("--voices", default=os.getenv("GSV_TTS_VOICES", ""))
    parser.add_argument("--base-gpt-model", default=os.getenv("GSV_TTS_BASE_GPT_MODEL", ""))
    parser.add_argument("--base-sovits-model", default=os.getenv("GSV_TTS_BASE_SOVITS_MODEL", ""))
```

> `create_gsv_tts_app` 需把 `build_voice_registry` 的 `GsvVoiceConfigError` 转成 `RuntimeError`，否则启动时报错信息不友好：
>
> ```python
>     try:
>         voice_registry = build_voice_registry(...)
>     except GsvVoiceConfigError as exc:
>         raise RuntimeError(f"Invalid GSV voice registry: {exc}") from exc
> ```

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_gsv_tts_experiment.py tests/test_gsv_voices.py -v
```

Expected: 全部 PASS（既有 `test_gsv_runtime_uses_upstream_infer_batched_contract` 必须仍然通过 —— 它不传 `voices`，走 legacy 分支）。

- [ ] **Step 5: Commit**

```bash
git add src/character_memory/gsv_tts_experiment.py tests/test_gsv_tts_experiment.py
git commit -m "Load and cache every registry voice in the GSV runtime

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 2.3: 按请求切换 voice

**Files:**
- Modify: `src/character_memory/gsv_tts_experiment.py`（`synthesize` :263-317）
- Test: `tests/test_gsv_tts_experiment.py`

**Interfaces:**
- Consumes: Task 2.2 的 `runtime.voices`
- Produces: `synthesize` 按 `request.voice` 选择参考与模型；未知 voice 抛 `ValueError`（→ HTTP 400）。

- [ ] **Step 1: 写失败测试**

```python
def test_synthesize_uses_the_requested_voice_reference(tmp_path):
    base_gpt = tmp_path / "base.ckpt"
    base_sovits = tmp_path / "base.pth"
    ref_a = tmp_path / "a.wav"
    ref_b = tmp_path / "b.wav"
    for path in (base_gpt, base_sovits, ref_a, ref_b):
        path.write_bytes(b"x")

    registry = build_voice_registry(
        '{"haru": {"ref_audio": "%s", "ref_text": "haru-ref"}, '
        '"momo": {"ref_audio": "%s", "ref_text": "momo-ref"}}' % (ref_a, ref_b),
        default_gpt=str(base_gpt),
        default_sovits=str(base_sovits),
        legacy_voice="",
        legacy_ref_audio="",
        legacy_ref_text="",
    )

    calls = []

    class FakeTts:
        def __init__(self, **kwargs):
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, *paths):
            pass

        def load_sovits_model(self, *paths):
            pass

        def cache_spk_audio(self, *paths, **kwargs):
            pass

        def cache_prompt_audio(self, **kwargs):
            pass

        def infer_batched(self, **kwargs):
            calls.append(kwargs)
            return (SimpleNamespace(audio_data=np.zeros(8, dtype=np.float32), samplerate=32000),)

    runtime = GsvTtsRuntime(
        gpt_model=str(base_gpt),
        sovits_model=str(base_sovits),
        ref_audio="",
        ref_text="",
        device="cpu",
        default_voice="haru",
        voices=registry,
        tts_factory=FakeTts,
    )

    runtime.synthesize(GsvTtsRequest(text="一", voice="haru"))
    runtime.synthesize(GsvTtsRequest(text="二", voice="momo"))

    assert calls[0]["spk_audio_paths"] == str(ref_a)
    assert calls[0]["prompt_audio_texts"] == "haru-ref"
    assert calls[1]["spk_audio_paths"] == str(ref_b)
    assert calls[1]["prompt_audio_texts"] == "momo-ref"


def test_unknown_voice_is_rejected(tmp_path):
    base_gpt = tmp_path / "base.ckpt"
    ref_a = tmp_path / "a.wav"
    for path in (base_gpt, ref_a):
        path.write_bytes(b"x")

    registry = build_voice_registry(
        '{"haru": {"ref_audio": "%s", "ref_text": "r"}}' % ref_a,
        default_gpt=str(base_gpt),
        default_sovits=str(base_gpt),
        legacy_voice="",
        legacy_ref_audio="",
        legacy_ref_text="",
    )
    runtime = GsvTtsRuntime(
        gpt_model=str(base_gpt),
        sovits_model=str(base_gpt),
        ref_audio="",
        ref_text="",
        device="cpu",
        default_voice="haru",
        voices=registry,
        tts_factory=lambda **kwargs: None,
    )
    with pytest.raises(ValueError, match="Unknown GSV voice"):
        runtime.synthesize(GsvTtsRequest(text="你好", voice="does-not-exist"))
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_gsv_tts_experiment.py -k "requested_voice or unknown_voice" -v
```

Expected: FAIL — 两个 voice 都用了同一份参考 / 未知 voice 未报错。

- [ ] **Step 3: 实现**

把 `synthesize` 开头 :264-269 替换为：

```python
        value = request.text.strip()
        if not value:
            raise ValueError("empty GSV-TTS text")
        voice_id = str(request.voice or self.default_voice).strip() or self.default_voice

        if self.voices:
            profile = self.voices.get(voice_id)
            if profile is None:
                allowed = ", ".join(sorted(self.voices))
                raise ValueError(f"Unknown GSV voice profile: {voice_id} (available: {allowed})")
            ref_audio = profile.ref_audio
            ref_text = profile.ref_text
            gpt_model = profile.gpt_model
            sovits_model = profile.sovits_model
        else:
            if voice_id != self.default_voice:
                raise ValueError(f"Unknown GSV voice profile: {voice_id}")
            ref_audio = self.ref_audio
            ref_text = self.ref_text
            gpt_model = self.gpt_model
            sovits_model = self.sovits_model
```

把 `infer_batched` 调用（:276-287）中的 4 个参数替换为局部变量：

```python
                    spk_audio_paths=ref_audio,
                    prompt_audio_paths=ref_audio,
                    prompt_audio_texts=ref_text,
```
和
```python
                    gpt_model=gpt_model,
                    sovits_model=sovits_model,
```

并把 `GsvTtsResult(..., voice=voice, ...)` 改为 `voice=voice_id,`。

> **注意**：若某 voice 的模型未被缓存（例如注册表在 load 之后变更），`infer_batched` 会因缺少模型而失败。当前设计在 `load()` 一次性缓存全部 voice，不存在该路径；不需要额外处理。

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_gsv_tts_experiment.py tests/test_gsv_voices.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: 用真实 sidecar 验证双 voice**

在 `.pytest-tmp/gsv-spike/` 造一个两 voice 的注册表 JSON（`murasame` 用微调模型，另一个用共享底模 + 一段参考 wav），用 `GSV_TTS_VOICES` 重启 sidecar，分别请求两个 voice，确认 `X-TTS-Voice` 与音色都正确切换。记录到 spike 笔记。

- [ ] **Step 6: Commit**

```bash
git add src/character_memory/gsv_tts_experiment.py tests/test_gsv_tts_experiment.py docs/superpowers/notes/2026-09-19-gsv-spike-results.md
git commit -m "Select GSV reference and models per request voice

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Phase 3：正式接入 `:8001`

Phase 3 结束时，`tts_provider: gsv` 在正式聊天链路可用，且未知 provider 不再静默降级。

### Task 3.1: 未知 provider 守卫（先做，这是安全网）

**Files:**
- Modify: `src/character_memory/media_server.py`（`/v1/tts` handler，`selected` 计算在 :170-172，fallthrough 在 :240-264）
- Test: `tests/test_media_runtime.py`

**Interfaces:**
- Consumes: 无
- Produces: 未知 `tts_provider` 返回 HTTP 400，而非静默返回 Sherpa 音频。后续 Task 3.2 依赖此守卫存在。

> **为什么先做**：`config.py:33` 的 pattern 一旦放开 `gsv`，`settings_store` 立刻允许保存；如果此时路由分支还没写，就会静默降级。先把守卫装上，任何中间态都是"明确报错"而不是"悄悄用错引擎"。

- [ ] **Step 1: 写失败测试**

在 `tests/test_media_runtime.py` 中新增（复用文件内既有的 `_ProviderClient` / `FakeTts` 构造模式）：

```python
def test_unknown_tts_provider_is_rejected_not_silently_downgraded():
    settings = SimpleNamespace(
        tts_provider="totally-unknown",
        tts_voice="0",
        tts_speed=1.0,
        tts_device="cpu",
        asr_provider="sherpa",
        db_path=":memory:",
    )
    app = create_media_app(provider_http_client=_ProviderClient(), settings=settings)
    with TestClient(app) as client:
        response = client.post("/v1/tts", json={"text": "你好"})
    assert response.status_code == 400
    assert "totally-unknown" in response.json()["detail"]
```

> 若 `create_media_app` 的实际签名与 `settings=` 不同，按 `tests/test_media_runtime.py` 中既有测试的注入方式调整（既有测试用 monkeypatch `media_server.load_settings`）。**以既有测试的写法为准**。

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_media_runtime.py::test_unknown_tts_provider_is_rejected_not_silently_downgraded -v
```

Expected: FAIL — 返回 200 且是 Sherpa 音频（这正是缺陷本身）。

- [ ] **Step 3: 实现**

在 `media_server.py` 的 `/v1/tts` handler 中，`explicit_voice` 赋值（:172）之后、第一个 `if`（:174）之前插入：

```python
        KNOWN_TTS_PROVIDERS = {"sherpa", "qwen3", "kokoro", "edge", "gsv"}
        if configured_routing and selected not in KNOWN_TTS_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown TTS provider: {selected}",
            )
```

> `HTTPException` 已在该文件 import（既有分支在用）。若没有，从 `fastapi` 引入。

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_media_runtime.py -v
```

Expected: 全部 PASS。既有 5 个 provider 测试必须仍然通过 —— 它们用的都是 `known` 集合内的 provider。

- [ ] **Step 5: Commit**

```bash
git add src/character_memory/media_server.py tests/test_media_runtime.py
git commit -m "Reject unknown TTS providers instead of silently serving Sherpa

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3.2: gsv 路由分支

**Files:**
- Modify: `src/character_memory/media_server.py`（kokoro/edge 分支 :207-238）
- Test: `tests/test_media_runtime.py`

**Interfaces:**
- Consumes: Task 3.1 的守卫；Lab 的 `POST /v1/tts`（`{"provider", "text", "voice", "speed"}`）
- Produces: `tts_provider: gsv` 时经 `{tts_lab_base}/v1/tts` 合成，响应头 `X-Media-Provider: gsv`，`content-type: audio/wav`。

- [ ] **Step 1: 写失败测试**

仿照 `test_configured_edge_tts_routes_mp3_through_media_runtime`（:339-362），新增：

```python
def test_configured_gsv_tts_routes_wav_through_media_runtime():
    settings = SimpleNamespace(
        tts_provider="gsv",
        tts_voice="murasame",
        tts_speed=1.0,
        tts_device="cuda",
        asr_provider="sherpa",
        db_path=":memory:",
    )
    client = _GsvProviderClient()   # 见 Step 3
    app = create_media_app(provider_http_client=client, settings=settings)
    with TestClient(app) as client_app:
        response = client_app.post("/v1/tts", json={"text": "你好"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["x-media-provider"] == "gsv"
    assert response.headers["x-media-voice"] == "murasame"
    assert response.content == b"RIFFgsv-wav"
    assert client.post_calls[-1][1]["json"]["provider"] == "gsv"
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_media_runtime.py::test_configured_gsv_tts_routes_wav_through_media_runtime -v
```

Expected: FAIL — `x-media-provider` 是 `sherpa`（守卫已挡住未知值，但 `gsv` 已在白名单内，仍会穿透到 Sherpa 分支）。

- [ ] **Step 3: 加 fake client 并实现路由**

在 `tests/test_media_runtime.py` 中仿 `_EdgeProviderClient` 增加：

```python
class _GsvProviderClient:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _ProviderStatusResponse(
            {"provider": {"id": "gsv", "ready": True, "loaded": True, "device": "cuda:0", "voices": ["murasame"]}}
        )

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _GsvResponse()
```

`_GsvResponse` 提供 `.content = b"RIFFgsv-wav"`、`.headers = {"content-type": "audio/wav", "x-tts-voice": "murasame", "x-tts-device": "cuda:0", "x-tts-inference-ms": "612.4", "x-tts-audio-ms": "2886.0", "x-tts-sample-rate": "32000"}`、`.is_error = False`（照抄 `_QwenProviderResponse` 的结构）。

在 `media_server.py` 中，把 :207 的分支条件：

```python
        if configured_routing and selected in {"kokoro", "edge"}:
```

改为：

```python
        if configured_routing and selected in {"kokoro", "edge", "gsv"}:
```

并把 :210 的默认 voice 与 :225 的 media type 回退改为对 gsv 也成立：

```python
            default_voice = "zh-CN-XiaoxiaoNeural" if selected == "edge" else ("murasame" if selected == "gsv" else "zf_001")
```

```python
            fallback_media_type = "audio/mpeg" if selected == "edge" else "audio/wav"
```

（`fallback_media_type` 一行原本就对非 edge 回退到 `audio/wav`，**无需改动**，此处仅确认。）

同理 :233 的 device 回退：

```python
                "X-Media-Device": response.headers.get("x-tts-device", "cloud" if selected == "edge" else settings.tts_device),
```

（原本对非 edge 用 `settings.tts_device`，**无需改动**。）

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_media_runtime.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/character_memory/media_server.py tests/test_media_runtime.py
git commit -m "Route gsv TTS through the provider lab path

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3.3: health 分支

**Files:**
- Modify: `src/character_memory/media_server.py`（`selected_tts_status` :64-131，关键是 :105）
- Test: `tests/test_media_runtime.py`

**Interfaces:**
- Consumes: Task 3.2 的 `_GsvProviderClient`
- Produces: `/health` 的 `tts` 字段对 gsv 报告 Lab 的真实状态，不再复用 Sherpa runtime 的 `ready`/`loaded`。

- [ ] **Step 1: 写失败测试**

```python
def test_configured_gsv_health_uses_provider_runtime():
    settings = SimpleNamespace(
        tts_provider="gsv",
        tts_voice="murasame",
        tts_speed=1.0,
        tts_device="cuda",
        asr_provider="sherpa",
        db_path=":memory:",
    )
    client = _GsvProviderClient()
    app = create_media_app(provider_http_client=client, settings=settings)
    with TestClient(app) as client_app:
        body = client_app.get("/health").json()
    assert body["tts"]["provider"] == "gsv"
    assert body["tts"]["ready"] is True
    assert client.get_calls == [("http://127.0.0.1:9002/v1/providers/gsv", {"timeout": 0.4})]
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_media_runtime.py::test_configured_gsv_health_uses_provider_runtime -v
```

Expected: FAIL — `body["tts"]["provider"]` 是 `gsv`（契约上会通过）但 `ready`/`loaded` 来自本地 Sherpa fake，且 **`get_calls` 为空**（因为 :105 的分支把它挡在了 Lab 探测之外）。断言 `get_calls` 是识别该缺陷的关键。

- [ ] **Step 3: 实现**

把 `media_server.py:105` 的：

```python
        if selected not in {"kokoro", "edge"}:
```

改为：

```python
        if selected not in {"kokoro", "edge", "gsv"}:
```

并把 :122 的 device 回退改为对 gsv 用上游值（与 edge 同构）：

```python
                "device": "cloud" if provider_id == "edge" else settings.tts_device,
```

（原本对非 edge 用 `settings.tts_device`；若希望 gsv 显示真实 `cuda:0`，改为优先取上游 `device` 字段 —— 以 Step 1 的断言为准调整。）

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_media_runtime.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/character_memory/media_server.py tests/test_media_runtime.py
git commit -m "Report gsv health from the provider lab, not the local runtime

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3.4: 配置取值与 Settings Center

**Files:**
- Modify: `src/character_memory/config.py:33`
- Modify: `src/character_memory/settings_store.py:65-70`
- Test: `tests/test_settings_center.py`

**Interfaces:**
- Consumes: 无
- Produces: `tts_provider` 接受 `"gsv"`；Settings Center 下拉可选。

- [ ] **Step 1: 写失败测试**

```python
def test_gsv_is_an_accepted_tts_provider(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("tts_provider: kokoro\n", encoding="utf-8")
    store = SettingsStore(config)
    store.save_values({"tts_provider": "gsv"})
    assert load_settings(config).tts_provider == "gsv"
```

> 按 `tests/test_settings_center.py` 既有的 `SettingsStore` 构造与 `load_settings` 用法调整（既有 round-trip 测试在 :62-88）。

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_settings_center.py::test_gsv_is_an_accepted_tts_provider -v
```

Expected: FAIL — pydantic `ValidationError`，pattern 不接受 `gsv`。

- [ ] **Step 3: 实现**

`config.py:33`：

```python
    tts_provider: str = Field(default="kokoro", pattern=r"^(kokoro|sherpa|qwen3|edge|gsv)$")
```

`settings_store.py:65-70` 的 provider options 增加一项（放在 edge 之后）：

```python
            {"value": "gsv", "label": "GSV-TTS-Lite (本地，角色音色)"},
```

> **不要**动 `settings_store.py:76-98` 的扁平 `tts_voice` 列表。

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_settings_center.py -v
```

Expected: 全部 PASS。若 `test_settings_center_and_formal_tts_wiring_are_declared`（:146-167）因读源码断言而失败，**更新其断言字符串**使其反映新状态（该测试的意图是"接线被声明过"，更新是预期内的）。

- [ ] **Step 5: Commit**

```bash
git add src/character_memory/config.py src/character_memory/settings_store.py tests/test_settings_center.py
git commit -m "Accept gsv as a configurable TTS provider

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3.5: dev_stack 白名单与 sidecar 启动

**Files:**
- Modify: `src/character_memory/dev_stack.py:71`（白名单）、`:209-238` 附近（qwen3 启动块之后）
- Test: `tests/test_dev_stack.py`

**Interfaces:**
- Consumes: Task 3.4 的 config pattern；`scripts/start-gsv-tts.sh`
- Produces: `tts_provider: gsv` 时 dev_stack 启动 GSV sidecar 并注入 `GSV_TTS_*`。**同时修复 `edge` 的既有遗漏。**

- [ ] **Step 1: 写失败测试**

```python
def test_dev_stack_starts_gsv_sidecar():
    from character_memory import dev_stack
    source = Path(dev_stack.__file__).read_text(encoding="utf-8")
    assert "gsv" in source
    assert "GSV_TTS_VOICES" in source
    assert '"http://127.0.0.1:9014/health"' in source


def test_dev_stack_provider_whitelist_includes_edge_and_gsv():
    from character_memory import dev_stack
    source = Path(dev_stack.__file__).read_text(encoding="utf-8")
    assert '"edge"' in source
    assert '"gsv"' in source
```

> 这两个测试沿用了该文件既有的"读源码断言"风格（`tests/test_dev_stack.py:4-27`）。这是**已知的弱测试**，但与该文件的既有约定一致，且能挡住"忘了加白名单"这个具体故障。

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_dev_stack.py -v
```

Expected: 新增的两个 FAIL。

- [ ] **Step 3: 实现**

`dev_stack.py:71` 的白名单：

```python
    return value if value in {"kokoro", "sherpa", "qwen3", "edge", "gsv"} else "kokoro"
```

在 qwen3 启动块（:209-238）之后，新增 gsv 块，结构仿照 qwen3：

```python
    if tts_provider == "gsv":
        gsv_root = ROOT / ".external" / "GSV-TTS-Lite"
        gsv_python = gsv_root / ".venv" / "Scripts" / "python.exe"
        if not gsv_python.is_file():
            gsv_python = gsv_root / ".venv" / "bin" / "python"
        env["CHARACTER_TTS_GSV_BASE"] = "http://127.0.0.1:9014"
        if gsv_python.is_file():
            specs.insert(
                1,
                {
                    "name": "gsv-tts",
                    "cmd": [str(gsv_python), "-m", "character_memory.gsv_tts_experiment"],
                    "cwd": str(ROOT),
                    "env": {
                        **env,
                        "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{gsv_root}",
                        "GSV_TTS_PORT": "9014",
                        "GSV_TTS_BASE_GPT_MODEL": os.getenv("GSV_TTS_BASE_GPT_MODEL", ""),
                        "GSV_TTS_BASE_SOVITS_MODEL": os.getenv("GSV_TTS_BASE_SOVITS_MODEL", ""),
                        "GSV_TTS_VOICES": os.getenv("GSV_TTS_VOICES", ""),
                        "GSV_TTS_PRELOAD": os.getenv("GSV_TTS_PRELOAD", "1"),
                    },
                    "health": "http://127.0.0.1:9014/health",
                },
            )
        else:
            print("[warn] GSV-TTS-Lite venv not found; skipping gsv sidecar", flush=True)
```

> **严格按 `dev_stack.py` 中 qwen3 块的实际结构改写**（spec 里的 `specs.insert` 签名、health gate 字段名以实际代码为准）。上面的 `env` / `specs` / `ROOT` 变量名取自该文件的既有用法；执行时以其真实命名为准。

- [ ] **Step 4: 运行确认通过**

```bash
uv run pytest tests/test_dev_stack.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: 端到端验收**

```bash
# 1. 配置 provider
#    在 config.yaml 设 tts_provider: gsv
# 2. 启动整套
uv run character-dev
# 3. 确认 sidecar 被拉起
curl -sS http://127.0.0.1:9014/health
# 4. 走正式路由合成
curl -sS -X POST http://127.0.0.1:8001/v1/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"test","voice":"murasame"}' \
  -D - -o .pytest-tmp/gsv-spike/formal.wav | grep -i x-media
```

Expected: `X-Media-Provider: gsv`，且 `.pytest-tmp/gsv-spike/formal.wav` 是 32kHz WAV。
（注意：中文文本用 `--data-binary @file`，内联 `-d` 在 Git Bash 下会被编码破坏并返回 400。）

- [ ] **Step 6: Commit**

```bash
git add src/character_memory/dev_stack.py tests/test_dev_stack.py
git commit -m "Start the GSV sidecar from dev_stack and fix the stale whitelist

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3.6: 把角色接到音色上（否则"每角色音色"不成立）

**Files:**
- Modify: `src/character_memory/gsv_tts_experiment.py`（`GsvTtsRequest` 与 `synthesize`）
- Modify: `src/character_memory/media_server.py`（gsv 分支）
- Modify: `src/character_memory/web/voice.js:584-598`
- Test: `tests/test_gsv_tts_experiment.py`、`tests/test_media_runtime.py`

**Interfaces:**
- Consumes: Task 2.3 的 voice 选择逻辑；Task 3.2 的 gsv 分支
- Produces: `GsvTtsRequest.fallback_voice: str | None`；浏览器发送 `voice: item.characterId`；媒体层在角色没注册音色时优雅退回默认音色。

> **为什么需要这个 Task**：做完 Task 2.x + 3.2 之后，GSV 能按 voice 切换了，但**没有任何东西会去切换它** —— `voice.js:591` 只发 `speaker_id`，从不发 `voice`；而 `media_server` 用的是全局 `settings.tts_voice`。结果是所有角色仍然共用同一个音色，"每角色音色"在端到端上不成立。
>
> **为什么用 `fallback_voice` 而不是让 sidecar 宽容**：Task 2.3 的严格 400 对 Lab 试听是有价值的（打错字应该报错）。正式链路则需要容错——不是每个角色都注册了音色。用一个显式字段区分这两种语义，比让 sidecar 全局变宽容更清楚。

- [ ] **Step 1: 写失败测试（sidecar 侧）**

```python
def test_fallback_voice_is_used_for_unknown_voice(tmp_path):
    base = tmp_path / "base.ckpt"
    ref = tmp_path / "a.wav"
    for path in (base, ref):
        path.write_bytes(b"x")

    registry = build_voice_registry(
        '{"murasame": {"ref_audio": "%s", "ref_text": "r"}}' % ref,
        default_gpt=str(base),
        default_sovits=str(base),
        legacy_voice="",
        legacy_ref_audio="",
        legacy_ref_text="",
    )

    calls = []

    class FakeTts:
        def __init__(self, **kwargs):
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, *paths):
            pass

        def load_sovits_model(self, *paths):
            pass

        def cache_spk_audio(self, *paths, **kwargs):
            pass

        def cache_prompt_audio(self, **kwargs):
            pass

        def infer_batched(self, **kwargs):
            calls.append(kwargs)
            return (SimpleNamespace(audio_data=np.zeros(8, dtype=np.float32), samplerate=32000),)

    runtime = GsvTtsRuntime(
        gpt_model=str(base),
        sovits_model=str(base),
        ref_audio="",
        ref_text="",
        device="cpu",
        default_voice="murasame",
        voices=registry,
        tts_factory=FakeTts,
    )

    # 未知角色 + 提供了 fallback -> 用 fallback，不报错
    result = runtime.synthesize(
        GsvTtsRequest(text="你好", voice="character-unknown", fallback_voice="murasame")
    )
    assert result.voice == "murasame"
    assert calls[0]["spk_audio_paths"] == str(ref)

    # 未知角色 + 没有 fallback -> 仍然报错（Lab 语义不变）
    with pytest.raises(ValueError, match="Unknown GSV voice"):
        runtime.synthesize(GsvTtsRequest(text="你好", voice="character-unknown"))
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_gsv_tts_experiment.py::test_fallback_voice_is_used_for_unknown_voice -v
```

Expected: FAIL — `GsvTtsRequest` 不接受 `fallback_voice`。

- [ ] **Step 3: 实现 sidecar 侧**

`GsvTtsRequest` 增加字段：

```python
    fallback_voice: str | None = Field(default=None, max_length=128)
```

`synthesize` 中，Task 2.3 写下的 voice 解析块里，`profile is None` 的分支改为：

```python
            if profile is None:
                fallback = str(request.fallback_voice or "").strip()
                profile = self.voices.get(fallback) if fallback else None
                if profile is None:
                    allowed = ", ".join(sorted(self.voices))
                    raise ValueError(f"Unknown GSV voice profile: {voice_id} (available: {allowed})")
                voice_id = profile.voice_id
```

- [ ] **Step 4: 实现媒体层与浏览器侧**

`media_server.py` 的 gsv 分支（Task 3.2 改过的 :207-217）：在 body 中让 gsv 带上 fallback。把该分支的 `json={...}` 改为：

```python
                    json={
                        "provider": selected,
                        "text": req.text,
                        "voice": voice,
                        "speed": speed,
                        **({"fallback_voice": str(settings.tts_voice or "murasame")} if selected == "gsv" else {}),
                    },
```

`web/voice.js:591` 的请求体：

```js
      body: JSON.stringify({text:item.text, speaker_id:speakerId, speed:1.0}),
```

改为：

```js
      body: JSON.stringify({
        text: item.text,
        speaker_id: speakerId,
        speed: 1.0,
        voice: item.characterId || undefined,
      }),
```

> `item.characterId` 在 `voice.js:586` 已被 `stableSpeakerId` 使用，可直接取用。
> `voice` 为 `undefined` 时 `JSON.stringify` 会省略该字段，`TtsRequest.voice` 保持 `None`，行为与今天完全一致 —— 对 kokoro/qwen3/edge 无影响。

- [ ] **Step 5: 运行确认通过**

```bash
uv run pytest tests/test_gsv_tts_experiment.py tests/test_media_runtime.py -v
```

Expected: 全部 PASS。既有 `test_configured_edge_tts_routes_mp3_through_media_runtime` 断言的 POST body 里**不应**出现 `fallback_voice`（edge 分支不受影响）。

- [ ] **Step 6: 端到端验收**

按 Task 3.5 Step 5 的方式，但请求体带 `"voice": "<某个已注册角色 id>"`，确认 `X-Media-Voice` 等于该角色 id；再带一个**未注册**的 id，确认优雅退回默认音色（200，`X-Media-Voice` 为默认音色）而不是 400。

- [ ] **Step 7: Commit**

```bash
git add src/character_memory/gsv_tts_experiment.py src/character_memory/media_server.py src/character_memory/web/voice.js tests/test_gsv_tts_experiment.py tests/test_media_runtime.py
git commit -m "Map character ids to GSV voices with a safe default

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 后续：Phase 4 单独成 plan

Phase 4（Lab UI 的"Qwen3 生成参考 → 试听 → 固化到 `personas/<name>/voice/`"）**不在本 plan 内**，原因有两个，都是实质性的：

1. **形态取决于 Task 0.2 的结论。** 若合成参考听感不可用，Phase 4 会变成"人工素材导入 + 剪辑"流程，与"Qwen3 合成"是两套 UI。
2. **需要对 `tts_lab.py` 的 UI 层与 `personas/` 目录约定做进一步勘察**，本 plan 未覆盖。

Task 0.2 完成后，据其结论另写一份 plan。

## 本 plan 的完成定义

Phase 0–3 全部 Task 勾完，且满足 spec §8 的验收标准 1、2、4、6：

| spec 验收标准 | 由哪个 Task 覆盖 |
|---|---|
| 1. `tts_provider: gsv` 返回真实 GSV 音频 | Task 3.2 + 3.5 Step 5 |
| 2. 未知 provider 明确报错，不静默降级 | Task 3.1 |
| 3. 输出稳定性 | Task 1.1–1.3（形态由 Task 0.1 结论决定） |
| 4. 多 voice 互不污染 | Task 2.3 + 3.6 Step 6 |
| 5. Lab UI 完成参考生成→固化 | **Phase 4**（另写 plan） |
| 6. `/health` 与实际路由一致 | Task 3.3 |

**端到端的"每角色音色"由 Task 3.6 补齐** —— 缺了它，Task 2.x 有能力但没有任何调用方会去用它。
