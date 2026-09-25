# Visual — Capture, Generation & Avatar

本文统一描述 Character Memory 的视觉域。视觉能力都属于同一个 Persistent Person，但必须保持三个方向的语义边界：

```text
Visual Capture = 看（Camera / Screen / Vision context）
Visual Generation = 画（ImageGen）
Avatar = 人物当前视觉身份资产
```

三个方向都收敛在本文件维护，避免同一视觉域能力分散。

## Visual Capture

Visual Capture 让 Browser 把摄像头或屏幕中的少量关键帧作为**当前一轮**视觉上下文交给同一个 Character。

最重要的概念边界：

```text
Visual Capture = 看
Visual Generation = 画
```

Capture 不生成新图，也不建立第二个视觉 Agent。它只是给现有 PersonRuntime 增加当前 turn 的 Vision context。

### 1. Supported sources

当前 Browser 支持：

```text
CAMERA
DISPLAY
```

- `CAMERA` 使用 `navigator.mediaDevices.getUserMedia()`；
- `DISPLAY` 使用 `navigator.mediaDevices.getDisplayMedia()`；
- audio 不随视觉 stream 上传。

Camera/Screen 可以在通话 UI 中开启，也可以复用同一 Visual Capture client contract 发送到 Direct / Group conversation。通话 Session、麦克风和视觉采集彼此独立：用户可以关闭麦克风后继续单独共享屏幕或摄像头，AI TTS 输出也不会因为本地麦克风关闭而停止。

### 2. Browser flow

核心前端：

```text
web/visual_capture.js
web/visual_client.js
web/voice.js
```

大致流程：

```text
Camera / Display stream
        ↓
periodic lightweight sampling
        ↓
change-based candidate cache
        ↓
select a few chronological keyframes
        ↓
JPEG data URLs
        ↓
visual message request
```

当前 `visual_capture.js` 的实现默认会：

- 大约每 `800 ms` 做一次候选采样；
- 将送往模型的长边压到约 `512 px`；
- 只在画面变化明显或经过一段时间后保留候选；
- candidate cache 有上限；
- 发送时优先保留首尾、变化较大的帧并保持时间顺序。

这些是当前实现参数，不是需要长期冻结的产品 API。稳定 contract 是“选择少量代表性关键帧，而不是连续上传视频”。

### 3. HTTP routes

后端入口：

```text
POST /v1/visual/direct/messages
POST /v1/visual/groups/{conversation_id}/messages
```

请求包含正常用户文字和 `visual_frames[]`。

每帧包含：

```text
filename
base64 image data URL
source: CAMERA | DISPLAY
captured_at_ms
```

Direct 还带 `character_id / conversation_id`；Group 使用 path 中的 conversation id，并继续支持 mention 解析。

### 4. Hard limits

后端固定保护：

```text
frames per request   <= 5
single frame         <= 2 MiB
total frame payload  <= 6 MiB
mime                 JPEG / PNG / WebP
```

后端会重新 base64 decode 并 sniff MIME，不信任 Browser 声明的文件类型。

超过任何限制都应在进入模型前返回明确 400，而不是继续把过大 data URL 塞进 LLM request。

### 5. Persistence boundary

Visual Capture frame bytes **不作为聊天附件长期保存**。

后端将用户文字正常持久化为 User Event，但 frame 二进制只存在于本轮 request/reaction context：

```text
User text
  -> durable Event

Visual frames
  -> transient image_data_urls
  -> ReactionScheduler
  -> same PersonRuntime / Vision model turn
  -> discarded after the turn
```

Durable Event 只保存少量 capture metadata，例如：

```json
{
  "visual_capture": {
    "frame_count": 4,
    "sources": ["CAMERA"],
    "captured_at_ms": {
      "first": 1234,
      "last": 4567
    }
  }
}
```

没有时间值时 `captured_at_ms` 可以省略。

这与普通 image attachment 不同：普通附件会进入 `MediaStorage / media_assets`，而 Visual Capture frame 不会。

### 6. Same PersonRuntime

Direct：

```text
User text + transient frames
  -> durable direct Event
  -> ReactionScheduler.enqueue_direct(... image_data_urls=frames)
  -> existing PersonRuntime
```

Group：

```text
shared user fact + transient frames
  -> conversation_events
  -> ReactionScheduler.enqueue_group(... image_data_urls=frames)
  -> existing GroupConversationService / member PersonRuntime
```

所以 Camera / Screen 不拥有独立 Memory、Mental State 或 Persona。

如果人物之后需要记住视觉内容，应由正常 Person reaction / Memory admission 形成语义 Memory，而不是把原始帧当 Memory。

### 7. Direct / Group semantics

#### Direct

Visual request 会保留用户原始文字作为 UI display text，并在模型 context 中附加“本轮同时提供实时视觉关键帧”的内部提示。

#### Group

Group visual request 继续使用共享 room fact 与普通 mention ordering。所有参与判断的成员看到的是同一次 room user fact，并可结合本轮 frames 理解。

frame bytes 不会被复制成每个 Character 各一份 durable fact。

### 8. Call integration

通话会话可以组合使用麦克风、Camera 和 Screen Capture，但三者不是绑定关系：

```text
Call Session
├─ microphone      optional / 可随时 mute
├─ camera/display  optional / 可独立保持
└─ AI TTS output   independent
```

麦克风 acquisition 与 Camera/Screen 遵守同一条规则（见 §9）：每次申请领一个序号，只有最新的申请才允许接管麦克风，`stopMicrophone`（挂断和静音都走它）让所有还挂在权限弹窗上的申请作废。挂断或静音之后才被回答的许可会当场 `stop()` 掉拿到的 track 并直接返回，不建 AudioContext、不改通话状态——否则麦克风会在通话结束后继续存活，而那时 overlay 已隐藏、麦克风按钮已 disabled，页面上没有任何控件能把它关掉。

语音输入仍保持原有顺序：

```text
speech
  -> ASR
  -> transcript validity gate
  -> only valid transcript selects/sends visual frames
```

当麦克风关闭而 Camera/Screen 仍在共享时，当前通话目标中的普通文字消息会自动选择最近约 15 秒内的少量关键帧，并走现有 visual message route。若用户切换到其他 Direct/Group conversation，视觉帧不会跟随到非通话目标。

当前 validity gate：

```text
trim 后空字符串        -> reject
纯空白/标点/符号        -> reject
任意汉字                -> accept
ASCII Latin/digit >= 2 -> accept
其它                    -> reject
```

例如：

```text
""      reject
"……"    reject
"?"     reject
"a"     reject

"嗯"     accept
"你好"   accept
"OK"    accept
"GPT"   accept
"123"   accept
```

无效 ASR：

- 不创建 chat message；
- 不上传当前视觉关键帧；
- UI 回到 listening。

这样可以避免一次纯噪声识别同时误提交一组 Camera/Screen 数据。

### 9. Lifecycle and privacy

Browser stream 生命周期由用户明确控制：

- Start Camera；
- Start Screen Share；
- Stop；
- 浏览器/系统结束 track 时自动停止。

切换来源会先停止上一条 stream，再开始新的来源。

权限弹窗可能在请求之后的任意时刻才被回答，所以**拿到 stream 不等于这次采集还该继续**。每一路 acquisition 都带一个申请序号，只有比当前 owner 更新的序号才允许接管会话；`stop()` 会自己领一个新序号，因此挂断瞬间所有还挂在弹窗上的申请全部作废。后到的 stream 一旦发现自己已经过期，就立刻 `stop()` 掉自己的 track（摄像头/屏幕指示灯熄灭），既不写入 preview 也不启动采样。

结论是每一条从浏览器拿到的 stream 只有两种结局：被会话接管，或者当场释放。不存在“还活着但没人认识它”的第三种结局，因为那意味着指示灯一直亮着，而页面上已经没有任何控件能把它关掉。

当前设计不做：

- 后台持续录像；
- raw video persistence；
- raw frame history browser；
- 把整个屏幕共享 session 上传服务器；
- 跨 turn 自动重用过去 capture bytes。

因此 Visual Capture 更接近“这一句话说出口时，我顺便给你看几张当前画面”，而不是监控/录像系统。

### 10. Failure boundary

Visual Capture 是可选输入能力：

- Camera/Screen 权限失败不应破坏纯文本聊天；
- frame validation 失败必须阻止该 visual request，而不是写入不可解释的坏 frame；
- source User Event 一旦成功接受，后续 LLM/Provider failure 仍遵守普通 durable-fact 优先级；
- Capture 与 ImageGen failure 相互独立。

### 11. Regression expectations

至少持续覆盖：

- Direct / Group route 都存在；
- 5-frame / 2-MiB / 6-MiB 限制；
- JPEG/PNG/WebP MIME validation；
- frame bytes 不进入 MediaAsset/Event payload；
- metadata summary 正确；
- Browser CAMERA / DISPLAY start/stop；
- 挂断后到达的权限许可不会留下活着的 track；
- selected frames 不超过后端上限；
- Voice invalid transcript 不发送 frames；
- valid transcript + capture 仍进入同一个 PersonRuntime。

相关长期回归清单见 [`EVALS.md`](EVALS.md)。

## Visual Generation

本文描述当前图片生成能力。它与“用户上传/采集图片给 Vision 看”是两个不同方向。

### 1. Visual Capture vs Visual Generation

```text
Visual Capture / Vision = 看
Existing image / Camera / Screen
  -> Person understands image

Visual Generation = 画
Instruction / Character intent
  -> Prompt compiler
  -> Image provider
  -> new image
```

不要把 Vision model routing、Camera/Screen Capture 和 ImageGen provider 混成一个概念。实时视觉输入见 [`VISUAL.md`](VISUAL.md)。

### 2. Visual purposes

当前 `VisualPurpose`：

- `AVATAR`
- `SELFIE`
- `SCENE`
- `STICKER`（provider contract 已保留，当前聊天自主链路不以它为主）

画布比例由程序控制：

```text
AVATAR  -> 1:1
SELFIE  -> 3:4
SCENE   -> 4:3
STICKER -> 1:1
```

LLM 不决定 Provider transport 参数。

### 3. Prompt compilation

`VisualPromptPlanner` 的职责很窄：

```text
Persona
+ Mental State
+ recent dialogue mood
+ visual intent
+ whether a reference exists
  ↓
plain-text final image prompt
```

它**不返回 JSON**。

不再要求 LLM 输出：

- aspect ratio
- provider
- identity constraint list
- persistence policy
- media ID

这些属于程序 contract。这样不会因为类似 `identity_constraints` 返回 string/list 不一致而让图片在真正调用 Provider 前失败。

### 4. Character-autonomous ImageGen

Person LLM 可以输出内部 action：

```json
{
  "type":"GENERATE_IMAGE",
  "image_purpose":"SELFIE",
  "visual_intent":"想自然分享一下现在的样子"
}
```

当前自主链路只支持：

- `SELFIE`
- `SCENE`

Direct `USER_MESSAGE` 与 Group 中各成员的 `USER_MESSAGE` reaction 都可以自主产生 `GENERATE_IMAGE`。是否画、画什么、以及 `visual_intent` 都由对应 Character 自己决定，不需要用户先打开生图工具或手写 Prompt。

同一 Character 单轮最多产生 1 个 `GENERATE_IMAGE`；不同群成员如果各自确实想画，可以分别产生自己的生成任务。

#### SELFIE

语义：人物想分享“自己”。

如果当前 Provider 支持 reference image，程序默认读取人物当前 avatar 作为 identity anchor。

Reference 是身份一致性工具，不意味着把头像像素复制粘贴进结果。

#### SCENE

语义：人物想分享一个场景、环境、氛围、设计稿或其它视觉表达。

**SCENE 不要求人物必须出镜。**

例如：

- 窗外的雨夜；
- 房间桌面；
- 她现在看到的街道；
- 想象中的某个场景；
- 根据群聊任务要求给出的界面稿、角色草图或概念设计。

当前自主 SCENE 不会为了“保持角色身份”机械附带 avatar reference。

如果未来真实需求证明设计稿需要独立画幅、reference 或 provider 策略，再扩 `DESIGN`；当前不为分类完整性提前增加 enum。

### 5. Direct and Group execution

#### Direct

Direct autonomous ImageGen 使用现有 Visual Runtime：

```text
PersonReaction
├─ MESSAGE -> normal transaction
└─ GENERATE_IMAGE -> internal tool intent

main reaction commits / SSE text
            ↓
      visual worker
            ↓
        provider.generate
            ↓
     MediaAsset + IMAGE Event
            ↓
         direct SSE
```

#### Group

Group 不建立第二套 ImageGen system。

当前 group adapter 复用：

- `VisualPromptPlanner`
- configured Image Provider
- MediaStorage
- `SELFIE / SCENE`
- avatar reference semantics
- stale-result guard

Group Character 主 reaction 先提交。生成完成后，图片以发起 `GENERATE_IMAGE` 的 Character 身份写入：

```text
conversation_events
```

并通过已有 group SSE 推送。

如果图片生成期间 room 已经收到更新的用户事实，旧结果会按 group user watermark 判 stale，不插回更新 turn。

### 6. Failure boundary

自主 ImageGen 是 secondary asynchronous output：

- 图片慢，不阻塞已经成立的文字表达；
- Provider 故障只记录 visual error，不回滚文字/Mental State；
- newer user fact 可以使旧生成结果 stale；
- 每个 Character 单轮最多 1 个自主生成任务；
- Wake/Proactive 当前不自动花费 ImageGen 配额。

成功图片 Event metadata 包含 generation purpose/provider/model/source event 等 provenance。

### 7. Explicit user image tool

用户有时不是在问“角色愿不愿意发自拍”，而只是明确需要一个绘图工具。因此正式提供：

```text
POST /v1/characters/{character_id}/images/rewrite
POST /v1/characters/{character_id}/images/generate
```

#### Rewrite

输入自然语言 instruction，例如：

```text
画一张 Rin 在图书馆窗边看雨的日常场景，安静一点
```

系统结合 Character Persona/状态做 AI prompt rewrite，返回最终 prompt，不调用图片 Provider。

#### Generate

默认执行：

```text
natural instruction
  ↓ AI rewrite
provider.generate
  ↓
data URL image draft
```

默认 `persist_result=false`：

- 不创建 Character Message；
- 不污染聊天历史；
- 浏览器把结果送进已有 Image Draft；
- 用户可以补一句 caption；
- 点击“发送图片”后才成为真正聊天事实。

这与 Ctrl+V 粘贴图片的最终 send path 相同。

### 8. Direct and Group tool UX

显式 AI 生图工具和 Character 自主 ImageGen 是两条不同路径。

#### Direct

当前 Character 自然提供 Persona/reference context；Character 自己也可以在 reaction 中选择 `GENERATE_IMAGE`。

#### Group

显式工具需要先选一个 Character 作为视觉参考人物，因为一个 Group 没有唯一 Persona/avatar。该路径生成的是**用户控制的 draft**，最终用户确认后再作为 group image message 发送。

自主路径则由群成员在各自 PersonReaction 中决定是否生成，用户不需要自己写 Prompt，也不需要额外点击“生成”。

### 9. Providers

#### Agnes

- Provider id: `agnes`
- API key: `AGNES_API_KEY`
- 支持 reference images
- 默认 model config: `agnes-image-2.1-flash`
- 请求 `/images/generations`

#### msimg / ModelScope

- Provider id: `msimg`
- API key: `MSIMG_API_KEY` 或 `MODELSCOPE_API_TOKEN`
- 依赖本地 `msimg==0.0.4`
- 当前 integration 不支持 reference images
- models 可通过 `msimg_models` 配置

`GET /v1/visual/providers` 的 `available` 对 msimg 主要描述本地 dependency/runtime 是否可用，不是每次都执行远程生成 API 探测。

### 10. Dev Console

`:8002/dev` 的 ImageGen card 使用真实 Character Runtime visual endpoints，不是 mock。

支持 Character、Provider、Purpose、instruction、avatar reference、AI rewrite、generate、prompt preview、provider/model/duration、Media ID、图片预览和将候选设为当前 avatar。

Dev 的生成结果可以持久化为测试 MediaAsset；正式 chat tool 默认先保持 draft。

### 11. Avatar generation

正式路由：

```text
POST /v1/characters/{character_id}/avatar/generate
POST /v1/characters/{character_id}/avatar/from-chat
```

Avatar Generate 不维护第二套 Prompt 编译器。它复用显式 Image 工具的 `ImageRewriteRequest -> compile_instruction -> VisualPromptPlanner` 路径，把用户补充偏好和有限画风 preset 一起润色成一个稳定的 AVATAR prompt。

一次请求可以生成 1~4 张候选（UI 默认 4）。同一批共享润色后的基础 Prompt，只允许构图、视角、轻微表情发生变化；身份与画风必须保持一致。Generate 只创建候选，不应无提示自动替换当前头像。

`from-chat` 可以从合法 MediaAsset 或 Character Image 设置当前 avatar，并把内容复制到 avatar storage，使当前头像不依赖源媒体文件未来是否仍存在。

### 12. Configuration

Non-sensitive ImageGen configuration stays in `config.yaml`：

```yaml
image_generation_provider: "agnes"
image_generation_timeout_seconds: 180
agnes_base_url: "https://apihub.agnes-ai.com/v1"
agnes_image_model: "agnes-image-2.1-flash"
msimg_models: "qwen"
```

Secrets belong in `.env` / Settings Center：

```dotenv
AGNES_API_KEY=...
MSIMG_API_KEY=...
## MODELSCOPE_API_TOKEN=...   # optional alternative supported by msimg path
```

Legacy `agnes_api_key` / `msimg_api_key` fields may still be migrated for backward compatibility, but new config examples should not put plaintext keys in YAML. See [`SETTINGS_CENTER.md`](SETTINGS_CENTER.md).

完整开发环境使用：

```bash
bash scripts/sync-all.sh
```

不要单独运行局部 `uv sync --extra image-generation` 来维护完整 dev venv；uv exact sync 会把没有包含在本次 extras 集合中的其它开发依赖移除。

### 13. Current boundaries

- Wake/Proactive 不自动生成图片。
- Group 自主 ImageGen 仅来自各成员自己的 reaction；系统不替人物强制画图。
- 每个 Character 单轮最多 1 个自主生成任务；不做自动无限重画/自动选最佳图。
- 不把 Prompt planner 重新复杂化为大 JSON schema。
- 不把 Visual Capture frame 当成 ImageGen reference 的默认长期资产；Capture 是当前 turn 的感知输入。
- 生成图的长期语义通过正常 Event/Memory provenance 进入人物历史，而不是把二进制本身当 Memory。

## Avatar Search & Avatar Sources

Avatar is a local Character asset. Current product supports several ways to obtain a candidate, but all selected avatars are ultimately copied into local avatar storage so current identity does not depend on an external URL remaining alive.

### 1. Current avatar sources

Current avatar can come from:

1. external image search candidate;
2. generated avatar candidate;
3. an existing chat/generated MediaAsset;
4. a Character Image Catalog asset.

Search and generation are different capabilities：

```text
Avatar Search
  -> short web image query
  -> external candidate
  -> validate/download
  -> local avatar asset

Avatar Generate
  -> character visual prompt
  -> ImageGen provider
  -> generated MediaAsset candidate
  -> explicit user selection
  -> local avatar asset
```

Neither path should silently replace the current avatar without user action.

### 2. Avatar Search flow

1. Web UI opens avatar manager from Character header/avatar.
2. Optional user text is a **preference hint**, not a raw search-engine query.
3. `AvatarIntentPlanner` asks the existing Character model what visual direction fits the person now, with bounded context:
   - Persona
   - current Mental State
   - at most a few recent short chat lines
   - optional preference
4. Planner returns compact search intent:
   - `visual_intent`
   - 1~3 short image-search queries
   - optional mood/style labels
5. Search service queries the primary query first and only spends extra provider requests when needed.
6. Provider results are normalized into opaque candidate IDs.
7. Browser chooses `search_id/candidate_id`, not arbitrary download URL.
8. Backend downloads the cached candidate, validates content type/size and stores a local avatar copy.
9. Character profile exposes a versioned local `/avatar/asset` URL.

If LLM planning fails, deterministic fallback search remains available so avatar management does not become unusable.

### 3. Privacy boundary

Persona, Mental State and recent dialogue can be used inside the local/server-side planner.

The external search provider only receives short generated search queries.

Planner instructions must not copy：

- user names/private identifiers；
- relationship secrets；
- long chat quotations；
- unrelated Memory content。

Avatar search intent is ephemeral tool context. It does not automatically become Character Memory or a normal PersonRuntime action.

### 4. Search configuration

Non-sensitive settings stay in `config.yaml`：

```yaml
search_provider: "searchapi"
search_country: "jp"
search_language: "zh-cn"
search_safe_search: "strict"
avatar_dir: ""
avatar_max_bytes: 8388608
```

Search credentials belong in `.env` or the Settings Center, not in new `config.yaml` examples：

```dotenv
SEARCHAPI_API_KEY=...
BRAVE_SEARCH_API_KEY=...
```

Effective secret precedence is documented in [`SETTINGS_CENTER.md`](SETTINGS_CENTER.md)：

```text
system environment > .env > legacy config.yaml secret
```

`search_api_key` in an old local config is only a backward-compatibility migration source. Settings Center migrates it to the provider-specific environment name and removes the plaintext field.

Brave alternative non-secret config：

```yaml
search_provider: "brave"
search_country: "ALL"
search_language: "zh"
search_safe_search: "strict"
```

### 5. Provider abstraction

Image search providers normalize results into a shared result shape including：

- original image URL；
- thumbnail URL；
- source page URL/domain；
- width/height when available。

This keeps SSRF/download validation, avatar persistence and UI selection independent from a specific search vendor.

`SearchProvider` is now shared infrastructure, not Avatar-owned infrastructure:

```text
SearchProvider
├─ search_images -> Avatar Search / Space image expression
└─ search_web    -> World Observation URL discovery

HeadlessBrowserWebFetcher
└─ rendered public-page observation for World Observation
```

This still does **not** expose a generic browser tool to Direct/Group chat. World Observation is a separate bounded product path, and raw page content is not automatically Memory or a chat message. Provider construction lives in `runtime_services.py`; `avatar_web.py` only consumes the already-composed service.

### 6. Generated avatar

Image generation routes：

```text
POST /v1/characters/{character_id}/avatar/generate
POST /v1/characters/{character_id}/avatar/from-chat
```

Generate reuses the same `ImageRewriteRequest -> compile_instruction -> VisualPromptPlanner` prompt-polish path as the explicit Image tool. The avatar manager can add one bounded style preset before polish:

- `AUTO` — let Persona/world setting decide the stable visual language;
- `ANIME_CLEAN` — clean anime illustration;
- `SOFT_ILLUSTRATION` — soft semi-realistic illustration;
- `NATURAL_PORTRAIT` — natural portrait treatment.

One request produces 1~4 **candidates** (UI default 4). The polished base prompt is shared across the batch; candidates only vary framing/view/expression slightly so style and character identity do not drift just to look different.

If Provider supports reference images, current avatar may be supplied as an identity anchor.

Generated results are candidates, never an automatic current avatar. The user explicitly chooses one generated/chat asset and copies it into avatar storage via `avatar/from-chat`.

ImageGen provider/config details are documented in [`VISUAL.md`](VISUAL.md).

### 7. Local ownership

Once selected, avatar state is copied under configured avatar directory (default derived from DB directory).

This means：

- current avatar does not point directly to a third-party CDN；
- deleting a source chat media later should not silently break selected avatar；
- sidebar/header/direct/group UI can all resolve the same local versioned avatar URL。

### 8. Boundaries

- Avatar is an asset, not a relationship score.
- Search result selection does not become long-term Memory by default.
- Search Provider is not exposed as arbitrary Character web browsing.
- Generated avatar does not auto-commit over current avatar.
- Current avatar reference can help SELFIE/AVATAR identity consistency, but SCENE generation does not need to force the person into every image.
- Camera/Screen Visual Capture is not an avatar source; it is transient Vision context for a current turn.
