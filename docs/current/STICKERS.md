# Stickers

Sticker 当前是 **application/global resource**，不是“每个 Character 独占一套资源”的新设计。

运行时仍兼容历史 character-local pack，因此必须区分：

```text
current ownership   = global application resource
legacy compatibility = persona-local manifests / old character-scoped routes
```

V1 保留兼容，不为了清理历史语义去破坏现有资源。

## 1. Runtime catalog

正式 runtime catalog 会合并：

```text
built-in default pack
+
global user-imported pack(s)
+
legacy character-local manifests
```

核心入口：

```text
load_global_sticker_catalog(...)
```

当前 source 描述为：

```text
default+global+legacy
```

同一个 global catalog 会刷新到 Direct / Group 使用的 PersonRuntime，不要求每个 Character 重新复制一份 imported sticker。

## 2. Storage

全局用户 Sticker 默认存放在配置的：

```yaml
sticker_dir: ""
```

为空时从 DB parent 推导默认目录。

全局 manifest：

```text
<sticker_dir>/manifest.yaml
```

内置 default pack 位于 package Web assets；legacy persona pack 仍可能位于：

```text
<persona_dir>/stickers/manifest.yaml
```

这些 legacy manifests 只是兼容输入，不改变当前 global ownership。

## 3. Public HTTP surface

### Catalog

```text
GET /v1/stickers
```

可选 `character_id` 仍被接受用于旧客户端兼容，但正式返回：

```json
{
  "scope": "global",
  "source": "default+global+legacy",
  "stickers": []
}
```

用户导入资源在 Direct / Group 中共享。

### Global asset

```text
GET /v1/stickers/{sticker_id}/asset
```

这是当前正式 asset route。

### Legacy asset route

```text
GET /v1/stickers/{character_id}/{sticker_id}/asset
```

仍保留给旧客户端，但读取的仍是当前 global catalog。不要据此重新把 Sticker ownership 解释成 character-owned。

## 4. Web ZIP import

正式 Web import：

```text
POST /v1/stickers/import
Content-Type: application/zip
```

兼容参数：

```text
character_id
filename
auto_tag
```

其中 `character_id` 即使由旧客户端发送，也**不会决定 storage ownership**。后端只用它做兼容校验；导入仍写全局 user library。

成功后 runtime 会重新加载 global catalog，并刷新已经加载的人物 Sticker resource。

## 5. Import metadata

导入 ZIP 优先读取：

```text
all_tags.json
```

或者一个/多个：

```text
tags.json
```

metadata row 可以提供：

- id
- filename/file
- 中文/英文标签
- aliases/tags
- description
- set/pack id
- display/pack name

如果 ZIP 没有 metadata：

```text
auto_tag=true + available AI tagger
  -> 可以对图片自动生成 label/tags/description

auto_tag=false
  -> reject
```

即使已有 metadata，字段语义不完整时也可以按需用 AI tagger 补齐。

AI tagging 是 import-time metadata enrichment，不是每次 Character 想发 Sticker 时再调用一个模型。

## 6. Import safety

导入器有明确限制：

```text
archive bytes       <= 64 MiB
uncompressed bytes  <= 160 MiB
files               <= 500
supported assets    png/webp/gif/svg/jpg/jpeg
```

ZIP member 会做 path traversal 防护，不接受 absolute path 或 `..` escape。

导入采用 validate-first + manifest-last publication：

```text
read/resolve/tag/validate all rows
        ↓
write immutable/content-addressed assets
        ↓
validate temporary manifest
        ↓
atomic replace manifest last
```

目标是避免中途失败后，runtime 看到“manifest 已更新但图片还没写完”的半导入状态。

如果最终 commit 失败，新创建但未被正式 manifest 引用的资产会尽量回滚。

## 7. Runtime selection

PersonRuntime 不允许模型凭空发任意 Sticker ID。

每轮：

```text
global catalog
  ↓
Sticker retrieval / available resources
  ↓
LLM may choose one real sticker_id
  ↓
resource validation
  ↓
STICKER action
```

未知、不存在或 asset file 丢失的 Sticker 不应该被当作合法 outward resource。

模型看到的是有限的可用资源及其语义标签，而不是整个文件系统。

## 8. Built-in vs imported vs legacy

### Built-in

随项目提供的 default pack，作为所有人物的基础资源。

### Global imported

当前正式用户扩展资源。Web import 和 CLI import 都写到 `sticker_dir`，所有人物/群聊共享。

### Legacy character-local

早期 persona-local manifest 仍会被 runtime 合并，避免已有资源突然消失。

这是兼容层，不是新资源应该继续采用的 ownership 模式。

## 9. CLI import compatibility

`sticker_import_cli.py` 当前已经与 global ownership 对齐：

```text
archive ZIP
  -> resolve_sticker_dir(settings)
  -> global manifest/assets
  -> load_global_sticker_catalog(...)
```

历史 `--character` 参数仍接受，避免已有本地脚本直接失效，但它只做 character id 兼容校验，并打印 deprecated 提示；**不会改变 storage ownership**。

因此当前事实源保持一致：

```text
Web import  -> global
CLI import  -> global
Runtime     -> default + global + legacy read compatibility
```

legacy persona-local manifests 继续只读兼容，不再作为新 CLI 导入目标。

## 10. Relationship to ImageGen

Sticker 与 ImageGen 是不同资源路径：

- `STICKER` action 选择已经存在的 Sticker resource；
- `GENERATE_IMAGE` 触发新的图片生成；
- `VisualPurpose.STICKER` 只是 provider contract 中保留的 purpose，不代表当前聊天会自动用 ImageGen 即时制造每个 Sticker。

如果未来要做“AI 现场生成 Sticker”，需要单独定义生成、审核、入库和复用语义，不能直接混进现有 Sticker retrieval。

## 11. Regression expectations

至少持续覆盖：

- built-in/global/legacy catalog merge；
- `/v1/stickers` 返回 global scope；
- legacy `character_id` 不改变 Web/CLI import ownership；
- CLI `--character` compatibility 不写回 persona-local library；
- asset path validation；
- ZIP size/file-count/path traversal 限制；
- metadata-present 和 AI-auto-tag 两类 import；
- manifest-last atomic publication；
- runtime catalog refresh；
- unknown Sticker ID 不成为合法 outward action；
- legacy asset route 继续兼容。

相关回归清单见 [`EVALS.md`](EVALS.md)。
