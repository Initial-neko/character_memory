# Archive

这里保存已经完成的历史交付说明。

## 重要：Archive 不是当前 contract

P0.x 文档记录的是某一时点的目标、非目标和实现边界。随着后续阶段完成，很多句子会自然失效，例如：

- “当前没有 SSE”；
- “本阶段不支持 Vision / Avatar / ImageGen”；
- “WebUI 使用同步 `/v1/chat`”；
- “每 5 秒 polling”；
- “需要 `uv sync --extra ...`”；
- “某个 P0 JS 文件拥有最终 renderer”。

这些内容仍然有历史价值，所以没有全部删除；它们可以帮助理解架构为什么演进成现在这样。

当前开发时请按以下顺序判断：

```text
source HEAD
  > docs/current/
  > README / config.example.yaml / pyproject.toml
  > archive milestone
```

## Milestones

`milestones/` 当前保存：

- P0.5 Chat Rhythm / Persona Creation
- P0.6 Proactive / Unread
- P0.7 Sticker / Intent
- P0.8 Vision / Image Message
- P0.9 Clipboard / Sticker Pack
- P0.10 Web Sticker Manager
- P0.12 Global Sticker / UI Refactor
- P0.14 Stability / Performance
- P0.15 Async Conversation Runtime
- P0.16 Message Search / Mentions
- P0.17A Runtime Core

新的 milestone delivery note 如果只是描述“这一版做了什么”，也应该直接放 Archive；只有长期仍成立的行为 contract 才进入 `docs/current/`。
