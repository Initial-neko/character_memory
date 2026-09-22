# Codebase Layout

这份文档回答一个问题：**我要改某个能力，应该从哪里开始看？**

仓库根目录保持标准 Python 项目结构，不把 `src/`、`tests/`、`personas/` 等再套一层无意义目录。真正需要导航的是 `src/character_memory/` 内部职责。

## Root

```text
.
├─ src/character_memory/    Python package
├─ personas/                character definitions
├─ docs/                    current / research / archive
├─ tests/                   regression tests
├─ evals/                   long-term behavior eval data
├─ scripts/                 dev/media/TTS utility scripts
├─ .github/workflows/       CI
├─ config.example.yaml
└─ pyproject.toml
```

## Core runtime packages

```text
src/character_memory/
├─ application/             application orchestration
├─ domain/                  Pydantic domain contracts
├─ runtime/                 PersonRuntime + context/resource selection
├─ memory/                  embedding + recall
├─ storage/                 SQLite core persistence/search/history
├─ llm/                     cloud model adapter
├─ life/                    frozen life/time simulation
└─ eval/                    eval runner
```

### `application/`

- `chat_service.py` — direct application service / per-character lock。
- `async_conversation.py` — ReactionScheduler、watermark、SSE hub。
- `group_conversation_service.py` — group member ordering、shared-room reaction、member fault isolation。
- `proactive_service.py` — persisted Intent execution。
- `wake_service.py` — process-local character wake opportunity。
- `clock.py` — RealClock / FixedClock。

### `domain/`

- `models.py` — `Event / Memory / PersonReaction / ActionDecision / ...`。

这里是 LLM structured-output 的主要 contract 边界。修改 action 语义时优先从这里看，而不是只改 Prompt。

### `runtime/`

- `person_runtime.py` — Event → Recall → LLM → derived persistence。
- `person_context.py` — Direct/Group/Space/World shared read-only Persona + Mental State + Recall + Recent Events snapshot。
- `context.py` — channel-aware compiled character prompt。
- `sticker_retrieval.py` — Sticker 候选检索。

### `memory/`

- `embedding.py` — deterministic / sentence-transformers / OpenAI-compatible embedding。
- `recall.py` — VectorRecall ranking。

### `storage/`

- `sqlite.py` — core tables、migration ledger、Event/Memory/Trace/Intent/Media persistence。
- `chat_history.py` — direct history read model。
- `message_search.py` — direct/group durable message search repository helpers。

## HTTP / Web route modules

当前有一些 `*_web.py` 留在 package 顶层。这是历史演进留下的薄 route layer，**V1 不为了目录美观搬动它们**，也不要继续把新的大型 domain/runtime 逻辑塞进 route 文件。

```text
api.py                  core FastAPI app + direct/common APIs
server.py               application assembly / attach routes
async_web.py            async message accept + SSE routes
group_web.py            group HTTP surface
history_web.py          history APIs
memory_web.py           minimal Memory Inspector / pin / forget / correct APIs
search_web.py           durable message search APIs
world_web.py            World Observation diagnostic HTTP surface
space_web.py            Character Space HTTP surface
avatar_web.py           avatar HTTP/search adapter
visual_web.py           ImageGen/visual tool HTTP surface
visual_capture_web.py   Camera/Screen transient Vision routes
wake_web.py             wake/debug HTTP surface
```

`server.py` 是查看 Character Runtime route 装配的最快入口；真正的 Search/Avatar/ImageGen/World infrastructure 在 `runtime_services.py` / `create_api()` 中先完成 composition。Feature route 之间不应依赖“谁先 attach”来获得 provider。

后续如果 route 数量继续明显增长，可以在大版本单独迁移到 `web_routes/` 或 `transport/http/`；当前不要制造全仓 import churn。

## Feature/runtime modules at package root

这些文件仍在顶层，但各自有明确 ownership：

```text
avatars.py                  avatar local persistence
avatar_intent.py            AI avatar-search planning
images.py                   character image catalog
stickers.py                 built-in/global/legacy sticker catalog + import
sticker_import_cli.py       global sticker import CLI + deprecated --character compatibility
search.py                   shared image/web search provider contracts
visual_generation.py        ImageGen providers + prompt compiler
visual_runtime.py           direct autonomous generated-image execution
group_autonomous_visual.py  group autonomous ImageGen glue/adapter
runtime_services.py          Search/Avatar/ImageGen/World composition root
browser_web.py               public headless Chromium renderer
world_observation.py         search discovery -> rendered WorldObservation
remote_media.py              SSRF-safe public image downloader
space_store.py               Character Space shared posts/comments/reactions/views + schedule ledger
space_media.py               ordered Space <-> MediaAsset relation
space_media_executor.py      Space SEARCH_IMAGE / GENERATE_IMAGE execution
space_autonomy.py            Space opportunity + World appraisal + Audience loop
media.py                    media asset storage/contracts
media_runtime.py            local ASR/Sherpa TTS providers/runtime
media_bootstrap.py          Windows/native media bootstrap
media_server.py             Media Runtime FastAPI + formal TTS router
tts_lab.py                  :9002 TTS Provider Runtime + Workbench
tts_registry.py             formal realtime TTS provider metadata/ids/defaults
voices.py                   per-persona voice.yaml registry contract
gsv_tts_experiment.py       isolated :9014 GSV sidecar adapter/runtime
config.py                   Settings model + character discovery
envfile.py                  .env read/write + precedence/atomic multi-key helpers
settings_store.py           config/env persistence + migration/backup
settings_server.py          :8003 Settings Center FastAPI + runtime apply orchestration
persona_builder.py          character draft/build flow
resource_metrics.py         local resource sampling
logging_utils.py            logging setup
time_utils.py               datetime helpers
ui.py                       read-only Streamlit inspector
```

### Visual ownership

```text
看：
visual_capture_web.py + web/visual_capture.js
  -> transient Camera/Display frames
  -> same PersonRuntime Vision turn

画：
visual_generation.py
  -> prompt/provider contract
visual_runtime.py
  -> direct autonomous execution
group_autonomous_visual.py
  -> group adapter + conversation_events persistence
visual_web.py
  -> explicit user ImageGen tool
```

不要把 Visual Capture 和 Visual Generation 合并成一个概念。

## Entrypoints

`pyproject.toml`：

```text
character-memory    -> character_memory.cli:main
character-media     -> character_memory.media_bootstrap:main
character-dev       -> character_memory.dev_server:main
character-settings  -> character_memory.settings_server:main
character-tts-lab   -> character_memory.tts_lab:main
character-stack     -> character_memory.dev_stack:main
```

开发日常优先：

```bash
bash scripts/setup-media-models.sh
uv run character-stack
```

只需要依赖同步时：

```bash
bash scripts/sync-all.sh
```

## Scripts

```text
scripts/sync-all.sh             canonical dev environment sync
scripts/setup-media-models.sh       canonical local Embedding/ASR/Sherpa/Kokoro setup
scripts/setup-tts-models.sh         compatibility wrapper -> setup-media-models.sh
scripts/prefetch_embedding_model.py explicit sentence-transformers cache prefetch
scripts/prefetch_tts_models.py      Kokoro model/voice prefetch
scripts/run-media.sh            standalone Media Runtime helper
scripts/benchmark_media.py      local ASR/TTS benchmark
scripts/cosyvoice_sidecar.py    optional Python 3.10 CosyVoice sidecar
```

不要在新文档里把 `setup-tts-models.sh` 当作主入口；它只保留兼容。

## Frontend

```text
src/character_memory/web/
```

当前正式 UI 没有 bundler。JS 按 feature 拆分，HTML 直接按顺序加载。

主要 ownership：

```text
app.js                  conversation/composer core
groups.js               group UX
realtime_reconcile.js   realtime reconciliation
persona.js              character UI
mentions.js             @ mention
group_settings.js       group settings
stickers.js             sticker UI/import
images.js               normal image draft/send
ai_images.js            generated image source
avatars.js              avatar management
search.js               message search
space.js                Character Space feed + per-character Space entry
space.css               Character Space layout
intent.js               intent UX
wake.js                 wake
dictation.js            speech-to-text
voice.js                call UI + ASR validity gate + optional Visual Capture
visual_capture.js       Camera/Display sampling/keyframe selection
visual_client.js        visual request helper
time_format.js          shared MM-DD HH:mm:ss timestamp formatter
```

重要原则：

- `app.js` 是 conversation/composer 的核心 owner；feature module 不重复注册相互竞争的主 submit handler。
- `images.js` 管普通图片 draft；`ai_images.js` 只负责生成来源，最终复用同一 image draft/send path。
- `groups.js` 管 group-specific UX，但 durable group facts 仍由后端 `conversation_events` 管理。
- Visual Capture frame bytes 只做本轮模型上下文，不变成普通图片附件。
- 历史 `p0_*.css` 仍被正式页面加载，是待整理样式债务；V1 不重命名。

## Life / Inspector

当前：

- `life/runner.py / simulator.py / ticker.py` 仍有 CLI/运行入口，不是 dead code；
- `ui.py` 仍通过 `uv run character-memory inspector` 提供 read-only developer inspector。

产品方向已经冻结它们的扩张，但 **V1 保留**。是否整体删除属于后续大版本，不做零碎删减。

## Tests

`tests/` 暂时保持单层文件名，因为很多 test fixture 使用 repository-root 相对路径，同时 CI 已经稳定运行。

不要仅为了“看起来分文件夹”一次性移动几十个测试文件。目录调整必须保持 test discovery 与 browser marker contract。

## 修改功能时的最短阅读路径

| 目标 | 建议先看 |
| --- | --- |
| LLM 回复/structured output | `domain/models.py` → `llm/client.py` → `runtime/person_runtime.py` |
| Direct async/SSE | `application/async_conversation.py` → `async_web.py` → `web/app.js` |
| Group chat | `group_store.py` → `application/group_conversation_service.py` → `group_web.py` → `web/groups.js` |
| Character Space | `space_store.py` → `space_autonomy.py` → `space_media_executor.py` / `world_observation.py` → `space_web.py` → `web/space.js` |
| Group autonomous ImageGen | `group_autonomous_visual.py` → `visual_generation.py` → `web/groups.js` |
| Memory/Recall/Governance | `runtime/person_context.py` → `memory/recall.py` → `storage/sqlite.py` → `memory_web.py` → `web/app.js` |
| Formal TTS registry/routing | `tts_registry.py` → `settings_server.py` / `media_server.py` → `tts_lab.py` |
| Character GSV voice | `voices.py` → `gsv_tts_experiment.py` → `tts_lab.py` VoiceDesign freeze |
| Sticker | `stickers.py` → `runtime/sticker_retrieval.py` → `web/stickers.js` |
| 用户图片/Vision | `media.py` → `api.py/async_web.py` → `web/images.js` |
| Camera/Screen Vision | `visual_capture_web.py` → `web/visual_capture.js` / `web/voice.js` |
| ImageGen | `visual_generation.py` → `visual_runtime.py` → `visual_web.py` → `web/ai_images.js` |
| Shared Search / World | `runtime_services.py` → `search.py` → `browser_web.py` / `world_observation.py` → `world_web.py` |
| Avatar | `runtime_services.py` → `avatars.py` / `avatar_intent.py` → `avatar_web.py` → `web/avatars.js` |
| Voice/ASR | `media_bootstrap.py` → `media_runtime.py` → `media_server.py` → `web/voice.js` |
| Formal TTS | `config.py` → `media_server.py` → `tts_lab.py` → `web/voice.js` |
| Settings | `envfile.py` → `settings_store.py` → `settings_server.py` → `web/settings.*` |
| Dev Console | `dev_server.py` → `web/dev*.{html,js,css}` |
| TTS Lab | `tts_lab.py` → `web/tts_lab.*` |
