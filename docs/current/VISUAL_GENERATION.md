# Visual Generation

本文描述当前图片生成能力。它与“用户上传图片给 Vision 看”是两个不同方向。

## 1. Two visual paths

```text
Existing image -> Vision -> Person understands image

Instruction / character intent
  -> Prompt compiler
  -> Image provider
  -> new image
```

不要把 Vision model routing 和 ImageGen provider 混成一个概念。

## 2. Visual purposes

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

## 3. Prompt compilation

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

## 4. Character-autonomous ImageGen

Person LLM 可以输出内部 action：

```json
{
  "type":"GENERATE_IMAGE",
  "image_purpose":"SELFIE",
  "visual_intent":"想自然分享一下现在的样子"
}
```

当前只支持：

- `SELFIE`
- `SCENE`

并且只允许 direct `USER_MESSAGE` 触发角色自主 ImageGen。

### SELFIE

语义：人物想分享“自己”。

如果当前 Provider 支持 reference image，程序默认读取人物当前 avatar 作为 identity anchor。

Reference 是身份一致性工具，不意味着把头像像素复制粘贴进结果。

### SCENE

语义：人物想分享一个场景、环境、氛围或配图。

**SCENE 不要求人物必须出镜。**

例如：

- 窗外的雨夜；
- 房间桌面；
- 她现在看到的街道；
- 想象中的某个场景。

当前自主 SCENE 不会为了“保持角色身份”机械附带 avatar reference。

如果未来要区分 `ENVIRONMENT / POV / CHARACTER_SCENE`，应先用真实需求证明 `SCENE` 语义不够，而不是提前扩 enum。

## 5. Async execution

角色主 Reaction 的文字/状态先提交，ImageGen 在独立 visual worker 中执行：

```text
PersonReaction
├─ MESSAGE -> normal transaction
└─ GENERATE_IMAGE -> internal tool intent

main reaction returns / SSE text
            ↓
      Visual Runtime worker
            ↓
        provider.generate
            ↓
     MediaAsset + IMAGE Event
            ↓
         direct SSE
```

因此：

- 图片慢，不阻塞已经成立的文字表达；
- Provider 故障只记录 visual error，不回滚文字/Mental State；
- Provider 生成期间用户又发了新事实时，可以通过 `still_current` 丢弃 stale image。

成功图片 Event metadata 包含 generation purpose/provider/model/source event 等 provenance。

## 6. Explicit user image tool

用户有时不是在问“角色愿不愿意发自拍”，而只是明确需要一个绘图工具。因此正式提供：

```text
POST /v1/characters/{character_id}/images/rewrite
POST /v1/characters/{character_id}/images/generate
```

### Rewrite

输入自然语言 instruction，例如：

```text
画一张 Rin 在图书馆窗边看雨的日常场景，安静一点
```

系统结合 Character Persona/状态做 AI prompt rewrite，返回最终 prompt，不调用图片 Provider。

适合复制到外部图片工具继续生成。

### Generate

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

## 7. Direct and Group tool UX

显式 AI 生图工具可以从聊天页面触发。

### Direct

当前 Character 自然提供 Persona/reference context。

### Group

工具需要先选一个 Character 作为视觉参考人物，因为一个 Group 没有唯一 Persona/avatar。

生成结果本身仍是**用户生成的 draft**，最终用户确认后作为 group image message 发送；它不等于某个群成员自主产生的 `GENERATE_IMAGE`。

当前没有开放“多个群成员各自在同一 group turn 自主 ImageGen”，避免 2~4 个成员同时消耗 Provider 成本和制造不清晰的 room ordering。

## 8. Providers

### Agnes

- Provider id: `agnes`
- API key: `AGNES_API_KEY`
- 支持 reference images
- 默认 model config: `agnes-image-2.1-flash`
- 请求 `/images/generations`

### msimg / ModelScope

- Provider id: `msimg`
- API key: `MSIMG_API_KEY` 或 `MODELSCOPE_API_TOKEN`
- 依赖本地 `msimg==0.0.4`
- 当前 integration 不支持 reference images
- models 可通过 `msimg_models` 配置

`GET /v1/visual/providers` 的 `available` 对 msimg 主要描述本地 dependency/runtime 是否可用，不是每次都执行远程生成 API 探测。

## 9. Dev Console

`:8002/dev` 的 ImageGen card 使用真实 Character Runtime visual endpoints，不是 mock。

支持：

- Character
- Provider
- Purpose
- instruction
- use avatar reference
- AI rewrite
- generate
- polished prompt
- provider/model/duration
- Media ID（Dev persistence）
- generated image preview
- 将候选设为当前 avatar

Dev 的生成结果可以持久化为测试 MediaAsset，便于后续 avatar/资源检查；正式 chat tool 默认先保持 draft。

## 10. Avatar generation

正式路由：

```text
POST /v1/characters/{character_id}/avatar/generate
POST /v1/characters/{character_id}/avatar/from-chat
```

Generate 只创建候选，不应无提示自动替换当前头像。

`from-chat` 可以从合法 MediaAsset 或 Character Image 设置当前 avatar，并把内容复制到 avatar storage，使当前头像不依赖源媒体文件未来是否仍存在。

## 11. Configuration

`config.yaml` / env：

```yaml
image_generation_provider: "agnes"
image_generation_timeout_seconds: 180
agnes_api_key: ""
agnes_base_url: "https://apihub.agnes-ai.com/v1"
agnes_image_model: "agnes-image-2.1-flash"
msimg_api_key: ""
msimg_models: "qwen"
```

Env overrides：

```text
AGNES_API_KEY
MSIMG_API_KEY
MODELSCOPE_API_TOKEN
```

完整开发环境使用：

```bash
bash scripts/sync-all.sh
```

不要单独运行 `uv sync --extra image-generation` 来维护完整 dev venv；uv exact sync 会把没有包含在这次 extras 集合中的其它开发依赖移除。

## 12. Current boundaries

- Wake/Proactive 不自动生成图片。
- 群成员自主 ImageGen 尚未开启。
- 不做自动无限重画/自动选最佳图。
- 不把 Prompt planner 重新复杂化为大 JSON schema。
- 生成图的长期语义仍应通过正常 Event/Memory provenance 进入人物历史，而不是把二进制本身当 Memory。
