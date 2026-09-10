# P0.8 — Vision + Image Messages

P0.8 把“图片”接入现有 Persistent Person Runtime，但不把聊天系统改造成图片生成产品。

## 目标

本轮解决两个不同问题：

1. 用户可以给人物发送真实图片，人物能够看懂图片后自然回应；
2. 人物可以把自己已有图片库中的图片作为 `IMAGE` Action 发给用户，包括在一个到期 Intent 被执行时主动发送。

本轮不负责“凭空生成一张新图片”。图片生成模型以后可以作为独立能力接入，并把生成结果写入同一媒体/图片消息协议。

## 模型路由

OpenCode Go 保持唯一在线 Provider：

```text
https://opencode.ai/zen/go/v1/chat/completions
```

模型按输入类型路由：

```text
纯文本 / Sticker / Intent
    -> deepseek-flash                 # DeepSeek V4.1 Flash

用户本轮附带真实图片
    -> deepseek-v4-flash-vision-exp   # DeepSeek V4 Flash Vision Exp
```

DeepSeek 官方 Vision 文档明确说明 `deepseek-v4-flash-vision-exp` 接受图文混合输入；OpenCode Go 也在同一个 Chat Completions 端点暴露该模型。当前实现因此没有自行假设 `deepseek-flash` 可以接图片，而是显式区分 text / vision model id。

两条路径继续共用：

- `x-opencode-session`
- `response_format={"type":"json_object"}`
- Pydantic `PersonReaction` 校验
- 同一个 Runtime / Memory / Mental State / Intent / Trace 体系

这样普通聊天不会因为接入 Vision 而一直承担图片模型的成本。

## 用户图片

WebUI 输入区新增图片按钮。当前支持：

- JPEG
- PNG
- GIF
- WebP
- 默认最大 8 MiB

浏览器读取文件后，以 base64 data URL 随本轮 `/v1/chat` JSON 发送。服务端会根据真实文件头识别格式，不信任浏览器声明的 MIME。

图片文件保存到：

```text
<db parent>/media/
```

也可以通过 `media_dir` 显式配置。

SQLite 只保存：

- media id
- character id
- original filename
- MIME
- local storage filename
- created_at
- size

Event metadata 只引用 media id 和显示元数据。

**base64 图片内容不会写入 Event、Memory 或 Runtime Trace。**

调用 Vision 时，模型收到 OpenAI-compatible Chat Completions content blocks：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "<compiled person context>"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  ]
}
```

Trace 中同一块会被脱敏成：

```text
data:image/png;base64,<base64 omitted>
```

避免数据库因图片聊天迅速膨胀。

## IMAGE Action

新的对外表达 primitive：

```json
{
  "type": "IMAGE",
  "image_id": "afternoon_tea"
}
```

它与 `STICKER` 的语义不同：

- `STICKER` 是聊天表情资源；
- `IMAGE` 是人物可以分享的照片/插画/截图等内容资源。

人物不能编造图片 ID。Runtime 只允许发送当前 Character 的 `Available Images` 中真实存在的资源。

如果模型返回不存在的 ID：

```text
DROP_UNKNOWN_IMAGE
```

该 Action 不会落成 Character Message，并在 Runtime Trace 的 `image_decisions` 中可见。

## Character Image Catalog

每个人物可以建立自己的图片库：

```text
personas/<character_id>/
  persona.yaml
  images/
    manifest.yaml
    tea.png
    cat.webp
```

示例 manifest：

```yaml
images:
  - id: afternoon_tea
    file: tea.png
    label: 下午茶照片
    tags: [日常, 分享, 下午茶]
    description: 人物偶尔会自然分享的一张下午茶照片

  - id: street_cat
    file: cat.webp
    label: 路边小猫
    tags: [猫, 路上看到, 分享]
```

Runtime Context 会得到：

```text
# Available Images
- afternoon_tea: 下午茶照片；适合：日常、分享、下午茶
- street_cat: 路边小猫；适合：猫、路上看到、分享
```

模型只能从中选择。

当前没有默认 Character Image Catalog，因此人物不会凭空拥有照片。给某个人物添加 `images/manifest.yaml` 和对应图片后，普通回复以及到期的 Proactive Intent 都可以自然选择 `IMAGE`。

## 主动发送图片

P0.6 的 Intent Dispatcher 不需要另造一套图片调度器。

```text
已有 Intent 到期
    -> PROACTIVE_INTENT Event
    -> PersonRuntime
    -> Persona + Memory + Mental State + Available Images
    -> MESSAGE / STICKER / IMAGE / silence
```

如果人物返回有效 `IMAGE`，Intent 状态按表达行为处理为 `EXECUTED`，聊天历史、未读红点、来源 Event 和 Trace 都沿用现有链路。

仍保留防骚扰约束：上一条主动消息还没得到用户回复时，不继续叠加新的主动消息。

## 图片历史与记忆

图片原始二进制不是 Memory。

当前 Event 会保留“用户发送了一张真实图片”的来源事实，模型可以在这一轮基于视觉内容写出有意义的 Memory Candidate。例如：

```text
[SHARED] 用户给 Momo 看了自己刚完成的数据治理架构图，他们讨论了其中的模型分层。
```

是否长期记忆仍由现有 Memory Admission 决定。

后续回忆依赖结构化/语言记忆，而不是每次把所有历史图片重新送入 Vision；这对长期聊天成本和上下文长度更合理。

## 明确不做

本轮不做：

- 图片生成；
- 外部搜图并自动发送；
- 搜索引擎图片作为 Runtime 依赖；
- 自动头像生成/轮换；
- 视频；
- 一轮多张用户图片；
- 把历史所有图片重复发送给 Vision；
- 新的空闲主动话题 scheduler。

这些能力以后都应复用 Event / Media / Action 体系，而不是各自建立第二套聊天协议。
