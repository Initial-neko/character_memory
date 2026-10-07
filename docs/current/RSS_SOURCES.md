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
DELETE /v1/rss/sources/{source_id}
POST  /v1/rss/sources/{source_id}/restore
POST  /v1/rss/sources/{source_id}/refresh
GET   /v1/rss/items
GET   /v1/rss/items/{item_id}
GET   /v1/rss/items/{item_id}/image?url=...
GET   /v1/rss/categories
```

### Subscription lifecycle

Cancelling with `DELETE /v1/rss/sources/{source_id}` retains the source ID and all historical articles, sets `cancelled_at`, disables fetching and returns `{source,unsubscribed:true,history_retained:true}`. Repeated cancellation is idempotent; an unknown source returns 404. Default source listing hides cancelled sources; `?include_cancelled=true` includes them for restoration UI. Existing article queries still include their history.

`POST /v1/rss/sources/{source_id}/restore` restores the same identity, enables collection and immediately fetches once, returning `{source,refresh}` just like creation. Adding a cancelled Feed URL also restores its original identity; adding an active duplicate remains an error. Fetch failure preserves the subscription and returns `refresh.ok=false`. Clients must not issue another automatic refresh after add/restore. PATCH is a temporary enable/disable switch and cannot restore cancellation (409). Manual refresh of a cancelled source returns 409.

The additive `rss/003-subscription-lifecycle` migration retains existing rows. Every cancel/restore increments a generation; a fetch that started before cancellation cannot write results or fetch status after cancellation or restoration. Historical articles committed before cancellation remain available.

### Article query behavior

`GET /v1/rss/items` supports three deterministic queries without a model:

- Today's latest: `?period=today`. Today means the publication date in UTC+08:00, midnight inclusive to next midnight exclusive. Fetch time never makes an old or undated article today's news.
- Related titles: `?q=React`. Literal substring match on the title only, ignoring ASCII case. `%` and `_` are literal characters, not SQL wildcards. Leading/trailing whitespace is stripped; max 200 characters.
- Article type: `?category=ai` (also `technology`, `development`, `product`). Categories match any of their preset title keywords. `/v1/rss/categories` returns IDs, labels, keywords and the matching rule (`title_matches_any_keyword`). A Latin keyword only matches on a word boundary, so `ai` does not hit "email" and `API` does not hit "rapid"; Chinese keywords match as substrings, since Chinese has no word boundary to anchor to. Types can overlap; this is not semantic classification.

Filters combine with AND and may also combine with `source_id`. `period` defaults to `all` for existing API clients; the desktop page explicitly requests `today` by default. Invalid period, category or cursor is rejected with HTTP 400/422.

The response retains `items` and adds `has_more`, `next_before_id`, and `query` (effective period, keyword, category, source, date and timezone). Each item retains its stable ID, source, title, summary, normalized feed content, image/original URL, publication time and fetch time. There is no automatic full-article extraction from the original website.

`limit` is 1–100 (default 60). For another page, send the preceding response's `next_before_id` and the same filters. The cursor resolves to the article's `(COALESCE(publication_time, fetch_time), id)` sort key, so out-of-order insertion does not drop articles. Undated items stay in `all` with fetch time as the sorting fallback and are explicitly labelled as undated in the UI.

Examples:

```text
/v1/rss/items?period=today&limit=30
/v1/rss/items?period=all&q=React
/v1/rss/items?period=today&category=ai
```

## UI

The V1 surface intentionally stays small:

1. RSS subscription list: add, enable/disable, manual refresh and status.
2. Information feed: two-column image/text cards, today's latest by default, all-history toggle, title search, preset type filters and incremental pagination. Narrow widths below 380px use one column.
3. Article detail: sanitized Feed HTML preserving paragraphs, headings, lists, links and inline images, with an explicit excerpt caveat, original link, retry on failure, keyboard activation and Escape/back return preserving the feed's filters, position and focus.

`content_html` is added by the repeatable `rss/002-rich-content` migration. `content_text` retains paragraph line breaks for API consumers. Refresh repairs existing articles in place without changing their IDs, first fetch or first publication times, keeping pagination sort keys stable. Old rows use their plain text until the next source refresh. Feed images are displayed through the item image endpoint, which only accepts URLs recorded on that article, checks public HTTP targets and redirects, applies the same size/deadline bounds and validates raster file signatures (PNG/JPEG/GIF/WebP/AVIF). Scripts, embedded frames, styles and event attributes are removed. Image failures remain visible with a retry action. Feeds providing only an excerpt or no image remain incomplete; no original-site scraping or invented cover is performed.

The main application's external-information entry remains directly below Space. Source management shows article counts, fetch/success times and concrete errors. Loading/empty/error states are separate, failed requests can be retried, and stale feed/detail/source-list responses cannot overwrite newer navigation. If a `today` pagination response reports a different effective date, the page reloads the first page instead of mixing dates. API consumers can apply the same rule using `query.date`. Closing an in-progress add dialog does not cancel the backend write; its late result updates subscriptions without closing a newly opened dialog. This layer still does not connect Characters or implement native Android screens.

No dashboards, recommendations, analytics, tag management or AI summaries are part of V1.
