# P0.10 — Web Sticker Manager

## 目标

把 Sticker 从开发者手工配置能力变成正式聊天能力：

- 用户在聊天页直接导入自己的 Sticker Pack；
- 已标注的包直接使用标签，不重复调用模型；
- 只有图片的包可以选择使用 Vision 自动生成标签；
- 导入成功后立即热刷新当前 Character 的 Runtime Sticker Catalog；
- 用户和 Character 共用同一套 Sticker Catalog；
- Character 可以单独发送 STICKER，也可以 MESSAGE + STICKER；
- 仍然允许不用 Sticker，不为了“可爱”强制发图。

## Web 入口

点击聊天输入框旁的 `☺`，Sticker Panel 右上角提供 `＋`。

选择 ZIP 后显示：

- 文件名和大小；
- `AI 自动补标签` 选项；
- 说明已有标签时不会重新标注；
- 导入状态和成功数量。

上传使用原始 `application/zip` 请求体，不把 ZIP 转成 base64 JSON，也不依赖 `python-multipart`。

## 推荐导入格式：已标注 ZIP

ZIP 可以任意分目录，只要图片文件名能够与 metadata 唯一对应。

```text
my-stickers.zip
├── all_tags.json
└── split_stickers/
    ├── happy.png
    ├── shocked.png
    └── sleepy.png
```

也可以每个 pack 自己带 `tags.json`：

```text
my-stickers.zip
├── happy_pack/
│   ├── tags.json
│   ├── happy.png
│   └── yay.png
└── sleepy_pack/
    ├── tags.json
    └── sleepy.png
```

`all_tags.json` / `tags.json` 均为 JSON 数组。推荐字段：

```json
[
  {
    "id": "happy_01",
    "filename": "happy.png",
    "set_id": "basic_emotions",
    "display_name": "基础情绪包",
    "tag_zh": "开心",
    "tag_en": "happy",
    "aliases": ["高兴", "好耶", "开心笑"],
    "description": "适合表达开心、认可或庆祝。"
  }
]
```

其中：

- `filename`：必需，用来定位图片；
- `id`：推荐，最终成为模型可选择的 `sticker_id`；
- `set_id` / `display_name`：推荐，用于 WebUI 分组；
- `tag_zh` / `tag_en` / `aliases` / `description`：用于人物理解和选择 Sticker；
- `category` 可选，也会进入语义标签。

已有 metadata 时，AI 自动补标签选项不会重做完整标签；只有缺失语义的行才需要 Vision。

## 简化导入格式：只有图片

也支持：

```text
my-stickers.zip
├── happy.png
├── shocked.png
└── sleepy.webp
```

如果没有 `all_tags.json` / `tags.json`：

- 勾选 `AI 自动补标签`：逐张调用当前 `vision_model`，生成 `label / tags / description`；
- 不勾选：拒绝导入，因为没有语义标签时 Character 无法可靠判断什么时候应该使用哪张 Sticker。

AI 自动标签不是聊天回复模型的猜测，而是导入阶段的一次性资产整理。标签生成后写入本地 `manifest.yaml`，后续聊天只使用这些持久化标签，不会每次发 Sticker 都重新看图。

当前 Vision 自动标签支持 JPEG / PNG / GIF / WebP；SVG 可以作为已标注 Sticker 使用，但没有 metadata 时不能靠 Vision 自动标签。

## 导入后的本地结构

无论原 ZIP 如何分目录，最终都归一化为：

```text
personas/<character_id>/stickers/
├── manifest.yaml
├── happy_01.png
├── shocked_01.webp
└── ...
```

程序内置默认 Sticker 仍然保留。Character-local Sticker 与默认包合并，同 ID 时 Character-local 版本覆盖默认版本。

## 用户与 Character 共用 Sticker Catalog

用户点击 Sticker 时：

```text
Web Sticker Panel
    ↓ sticker_id
POST /v1/chat
    ↓
USER_MESSAGE Event
    ↓
模型上下文中的结构化 Sticker 语义
```

Character 回复时：

```text
# Available Stickers
- happy_01: 开心；适合：开心、高兴、好耶
- shocked_01: 震惊；适合：震惊、意外、懵

模型
    ↓
{"type":"STICKER","sticker_id":"shocked_01"}
    ↓
Runtime 校验真实 ID / 真实文件
    ↓
CHARACTER_MESSAGE Event
    ↓
WebUI 小尺寸 Sticker
```

因此 Sticker 不是“只给用户发送”的资源。它属于当前 Character 的表达资源，普通回复和到期的主动 Intent 都可以选择它。

## 热刷新

P0.9 CLI 导入需要重启进程才能保证运行中 Runtime 重新读取 Catalog。

P0.10 Web 导入在成功写入 `manifest.yaml` 后会立即：

1. 重新读取合并后的 Sticker Catalog；
2. 替换 `runtime.sticker_catalog`；
3. 返回最新 Sticker 列表；
4. 前端清除当前 Character 的 Sticker cache 并重新渲染。

所以 Web 导入成功后，新 Sticker 对用户和 Character 都立即可用。

## 安全边界

沿用 P0.9 ZIP 安全限制：

- 压缩包最大 64 MiB；
- 最大 500 个文件；
- 解压总体积最大 160 MiB；
- 拒绝 `../` 等路径穿越；
- 只导入支持的图片扩展名；
- `sticker_id` 归一化；
- Manifest 最后写入，避免半完成状态被 Runtime 读取。

AI 自动标注会产生 Vision 模型调用，因此 WebUI 明确显示该选项；已有完整标签时不会产生额外调用。
