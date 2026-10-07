# RSS / External Sources V1

RSS/Atom is the first deterministic external-information ingestion path.

## Boundary

```text
RSS / Atom
  -> fetch
  -> parse / normalize
  -> deduplicate
  -> durable rss_items
  -> Sources UI
```

V1 deliberately stops here.

RSS ingestion does **not**:

- call the Person model;
- create Memory;
- create WORLD_OBSERVATION;
- publish Space posts;
- send Direct messages;
- expose a generic Tool to a Character.

This keeps external collection separate from Character cognition. A later change can decide how selected `rss_items` become perception input without changing RSS storage.

## Default subscriptions

`rss_seed_default_sources` is `false`. A fresh database starts with an empty subscription list, and upgrading never inserts anything on its own. Setting it to `true` seeds:

- 阮一峰的网络日志 — `https://www.ruanyifeng.com/blog/atom.xml`
- AIHOT 日报 — `https://aihot.news/feed/daily.xml`
- hex2077.dev — `https://hex2077.dev/rss-zh-CN.xml`
- OpenAI News — `https://openai.com/news/rss.xml`

Seeding is opt-in because it writes into whatever database is already there, and the scheduler then starts talking to those hosts without the user asking for them.

`rsshub.app` itself is not a feed. Users can add any concrete RSSHub route through the normal “添加订阅” dialog.

## Runtime

`RssScheduler` is process-local and polls for sources whose own fetch interval has expired. Fetching starts after the first poll instead of blocking application startup. Manual refresh remains immediate.

User-supplied URLs pass the same public-http target guard used by existing remote media paths. Redirect destinations are checked again.

One fetch is bounded twice: a 4 MiB response budget enforced while the body is still streaming, and a whole-fetch deadline (`total_timeout_seconds`, 20 s) that also covers its redirects. The per-operation httpx timeout alone cannot bound a peer that keeps sending just inside the read window.

## HTTP

```text
GET   /sources
GET   /v1/rss/sources
POST  /v1/rss/sources
PATCH /v1/rss/sources/{source_id}
POST  /v1/rss/sources/{source_id}/refresh
GET   /v1/rss/items
GET   /v1/rss/items/{item_id}
```

## UI

The V1 surface intentionally stays small:

1. RSS subscription list: add, enable/disable, manual refresh and status.
2. Information feed: compact image/text cards inspired by Xiaohongshu density.
3. Article detail: normalized text plus original-link escape hatch.

No dashboards, recommendations, analytics, tag management or AI summaries are part of V1.