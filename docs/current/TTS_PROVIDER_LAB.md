# TTS Workbench / Provider Runtime

`:9002` is both the TTS Provider Runtime used by formal routing and the browser Workbench used for audition/benchmark and VoiceDesign tooling.

The combination is intentional for V1. Provider lifecycle and Workbench UI can be split later only if their operational requirements actually diverge.

## 1. Runtime map

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

## 2. Stable browser contract

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

## 3. Sherpa audition isolation

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

## 4. Health inventory and Settings

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

## 5. Canonical setup

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

## 6. Kokoro

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

## 7. Edge TTS

Edge runs inside `:9002` and is online-only. It needs no API key but synthesis requires Microsoft Edge TTS network access.

Default voice:

```text
zh-CN-XiaoxiaoNeural
```

Other configured Chinese voices come from the central registry. Edge returns MP3 and the Media/Browser chain preserves `audio/mpeg`; no unnecessary WAV transcode is inserted.

Health only verifies that the client dependency exists; it does not make a public-network call on every health probe.

## 8. GSV-TTS-Lite

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

### Voice templates and the character registry

A voice is a template, and a character only names one:

```text
voices/<name>.yaml                    # ref_audio + ref_text: the only carrier
voices/<name>/<content-addressed>.wav
personas/<character>/voice.yaml       # one line: template: <name>
```

The browser sends the Character id as the requested voice for GSV. If that id resolves in the GSV registry, the template it names is used; otherwise GSV falls back to the default template (`GSV_TTS_VOICE`) instead of muting the character.

VoiceDesign freeze stores the exact auditioned WAV and transcript as a template, rewrites the character's `voice.yaml` to name it, then asks the GSV sidecar to reload the registry without unloading the warm GPT/SoVITS engine.

Detailed behavior and determinism measurements live in `GSV_TTS_EXPERIMENT.md`.

## 9. Qwen3 boundary

The old Qwen3-TTS 0.6B sidecar and benchmark scripts remain for manual experimentation, but there is no `Qwen3SidecarProvider` in the Workbench runtime and `qwen3` cannot be selected as formal chat TTS.

Qwen3 1.7B VoiceDesign at `:9015` is a separate tool. Workbench prompt polish uses the public `PersonModel.complete_text_for_session()` contract and the normal configured Character Memory LLM; it does not depend on the OpenAI adapter's private transport method.

The freeze flow is described in `QWEN3_VOICE_DESIGN_TOOL.md`.

## 10. CosyVoice

CosyVoice stays isolated in its Python 3.10 sidecar at `:9012`. It is not required by the main stack and is not a formal chat Provider. If absent, the Workbench shows it unavailable and all formal Providers remain usable.

## 11. Current synthesis policy

GSV currently returns whole WAV output through `infer_batched`; formal Character voice playback is not token/chunk streaming. Browser voice overlaps playback of the current message with synthesis of the next queued message, preserving one-at-a-time playback.

Real performance acceptance should use the actual formal path:

```text
Browser/:8001 -> :9002 -> provider
```

and distinguish model inference, HTTP time, audio duration, RTF and first-playable-audio latency where applicable.

## 12. Testing boundary

CI verifies:

- formal provider registry/config validation;
- `:8001` formal routing;
- direct Sherpa audition bypassing the formal selector;
- Settings Provider/Voice/Device browser interaction;
- provider API shapes and optional-provider isolation;
- VoiceDesign freeze/registry contracts.

CI does not prove subjective voice quality or real CUDA latency. Those remain Windows/GPU acceptance tests.
