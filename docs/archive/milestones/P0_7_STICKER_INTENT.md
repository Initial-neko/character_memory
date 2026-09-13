# P0.7 — Sticker, Intent Preview & DeepSeek V4.1 Flash

本阶段不扩张 Life / Relationship State / Vision，而是把三个直接影响当前验证体验的点做完整：表情包、Intent 可观测性、默认聊天模型升级。

## 1. Character Summary 仍是单次批量请求

未读红点继续使用：

```text
GET /v1/characters/summaries
```

一次返回全部 Character 的最新聊天摘要和 latest assistant message id。前端不会对每个人物分别请求，也不会因为该轮询调用 LLM。

## 2. DeepSeek V4.1 Flash

默认 OpenCode Go 模型 ID 改为：

```text
deepseek-flash
```

Provider、endpoint、session header 和 structured JSON 调用路径不变。

当前只把它作为默认文本聊天模型使用；不假设其支持图片输入。任意图片 / Vision 后续作为独立 Media 能力处理。

## 3. Intent Preview

Intent 仍由正常 PersonReaction 的 `intent_candidates` 产生，不增加第二次“Intent 生成”调用。

新写入的 Intent 增加：

```text
source_event_id
```

现有 SQLite DB 启动时自动补列，不需要清库。

WebUI 顶部新增 `Intent`：

- PENDING：等待时间或条件；
- PROCESSING：正在执行；
- EXECUTED：已经形成主动表达；
- SUPPRESSED：到期后人物决定不表达；
- DEFERRED：人物决定延后；
- EXPIRED：错过窗口；
- ERROR：执行失败。

每条 Intent 展示 content / created_at / earliest_at / expires_at / reason；有 `source_event_id` 时可直接打开产生它的 Runtime Trace。

Intent Preview 是 Developer UX，不作为“关系进度”或游戏化系统展示给普通人物逻辑。

## 4. Sticker Contract

`ActionType` 新增：

```text
STICKER
```

Contract：

```json
{"type":"STICKER","sticker_id":"round_cat_happy"}
```

`STICKER` 不携带任意 URL，也不允许模型编造文件名。

每轮 Context 增加：

```text
# Available Stickers
- round_cat_happy: 圆猫·开心；适合：开心、可爱、欢迎、好耶
...
```

模型只能从这里选择 id。Runtime 在持久化前再次验证 id 和本地资源；不存在的 id 被丢弃，并在 Trace 中记录 `DROP_UNKNOWN_STICKER`。

## 5. Sticker Catalog

人物自己的资源优先：

```text
personas/<character_id>/stickers/
├── manifest.yaml
├── happy.webp
├── speechless.gif
└── sleepy.png
```

如果没有人物专属 Manifest，则使用程序包中的 default Sticker Catalog。

支持：

```text
PNG / WebP / GIF / SVG / JPG
```

默认包提供 8 个原创测试 Sticker，覆盖圆鸭和圆猫的典型聊天反应：开心、震惊、无语、躺平、期待、困、生气等。

本阶段对“小刘鸭 / 蜜桃猫”的研究只用于确认优秀聊天表情包应覆盖的情绪和反应范围。第三方 IP 图片不直接复制进仓库；正式使用第三方素材需要用户提供有权使用的文件或授权。

## 6. 用户发送 Sticker

WebUI 输入框增加 Sticker 按钮。点击后加载当前人物可用 Catalog；选择后作为一个独立用户消息发送：

```json
{
  "character_id":"momo",
  "conversation_id":"...",
  "message":"",
  "sticker_id":"round_duck_shock"
}
```

Event Log 保留 Sticker ID。为了让当前文本模型理解 Sticker，Runtime 输入中使用受控语义描述：

```text
[用户发送表情包：圆鸭·震惊；含义：震惊、意外、懵、什么]
```

聊天历史显示真实图片，而不是把这段内部语义文本展示给用户。

本阶段只支持“单独发一个 Sticker”，不做文字 + Sticker 同一次用户输入组合；连续发消息和表情包已经能覆盖当前聊天验证。

## 7. 人物发送 Sticker

人物可以自然产生：

```text
MESSAGE
STICKER
MESSAGE + STICKER
STICKER only
```

Sticker 和 MESSAGE 一样属于 `actions[0..3]`，使用现有 delivery rhythm；Sticker 使用较短展示间隔。

主动 Intent 到期时也允许发 Sticker，并且仍保留 `source_event_type=PROACTIVE_INTENT`，因此红点与主动消息 provenance 不受影响。

## 8. 当前不做

本阶段明确不做：

- 任意用户图片上传；
- Vision model routing；
- 搜索引擎自动下载 Sticker；
- 小刘鸭 / 蜜桃猫第三方素材内置；
- Avatar Pool / 自动换头像；
- 空闲时 Initiative Opportunity；
- Relationship State。

先利用 Intent Preview 判断“为什么人物不主动”，同时验证 Sticker 是否显著改善角色表达自然度，再决定下一步。
