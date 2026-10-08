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

Collection deliberately stops at shared storage. The optional personal reading
path below consumes selected local items through World; it does not change ingestion.

RSS ingestion does **not**:

- call the Person model;
- create Memory;
- create WORLD_OBSERVATION;
- publish Space posts;
- send Direct messages;
- expose a generic Tool to a Character.

This keeps external collection separate from Character cognition. Merely collecting
an article never establishes that any Character read it.

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

Feeds containing DOCTYPE/ENTITY declarations are rejected before XML parsing, and article/origin URLs are normalized to credential-free HTTP(S). During shutdown, in-flight fetching may finish its network read, but it cannot write to the closed SQLite store. The HTTP client is closed when the last active fetch returns.

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

1. RSS subscription list: add, enable/disable, manual refresh, confirmed cancellation and restoration (including historical article retention), and status.
2. Information feed: two-column image/text cards, today's latest by default, all-history toggle, title search, preset type filters and incremental pagination. A sidebar lists real subscriptions and their article counts. Selecting a source opens its full history and scopes search, type filters and pagination to that source; selecting all subscriptions restores today's view. Source changes reset pagination and ignore older responses, while retaining keyword/type filters. Narrow widths below 380px use one column.
3. Article detail: sanitized Feed HTML preserving paragraphs, headings, lists, links and inline images, with an explicit excerpt caveat, original links in the header and below the content, retry on failure, keyboard activation and Escape/back return preserving the source, other feed filters, position and focus. The bottom original link opens the article's HTTP(S) URL in a new tab; missing/unsafe URLs show an unavailable notice instead.

`content_html` is added by the repeatable `rss/002-rich-content` migration. `content_text` retains paragraph line breaks for API consumers. Refresh repairs existing articles in place without changing their IDs, first fetch or first publication times, keeping pagination sort keys stable. Old rows use their plain text until the next source refresh. Feed images are displayed through the item image endpoint, which only accepts URLs recorded on that article, checks public HTTP targets and redirects, applies the same size/deadline bounds and validates raster file signatures (PNG/JPEG/GIF/WebP/AVIF). Scripts, embedded frames, styles and event attributes are removed. Image failures remain visible with a retry action. Feeds providing only an excerpt or no image remain incomplete; no original-site scraping or invented cover is performed.

The main application's external-information entry remains directly below Space. Source management shows article counts, fetch/success times and concrete errors. Loading/empty/error states are separate, failed requests can be retried, and stale feed/detail/source-list responses cannot overwrite newer navigation. If a `today` pagination response reports a different effective date, the page reloads the first page instead of mixing dates. API consumers can apply the same rule using `query.date`. Closing an in-progress add dialog does not cancel the backend write; its late result updates subscriptions without closing a newly opened dialog. This Sources UI does not claim personal Character readership or implement native Android screens.

No dashboards, recommendations, analytics, tag management or AI summaries are part of V1.

## Optional Character personal reading

`world_rss_reading_enabled` defaults false and requires Character Runtime restart.
When enabled, the existing World BROWSE opportunity can offer up to eight local
items from enabled, noncancelled sources. The character chooses `NO_ACTION`,
`WEB_SEARCH` or `READ_RSS`; choosing RSS can select up to two offered IDs. A
single bounded batch appraisal processes all successfully read items. There is
no original-site fetch, per-item summary model, additional Scheduler or automatic
Memory, Direct message or Space publication. Disabled or empty candidates retain
the original Web schema/prompt and model call count. The existing daily Browse
budget and idle gate apply. Saving mode reduces RSS candidates to four and
selected items to one, preserving the same appraisal semantics.

`world_rss_readings` is a character-local ledger keyed by character and stable
item ID, separate from shared `rss_items`. Local reading atomically checks source
enabled/cancellation/generation state, claims the personal item and records the
actual text snapshot. Only IDs from the saved candidate set may execute. A
cancel/restore after selection invalidates the old generation. Concurrent
opportunities cannot read the same personal item. Another character has its own
independent chance to select it.

Each receipt retains source and item identity, original URL, Feed publication and
actual read times, a hash of the stored title/URL/summary/text, the number of
characters read/available and truncation status. At most 12,000 text characters
are read across a batch. `RSS_FEED_TEXT` explicitly means local Feed content,
which may only be an excerpt; it never means full original-page readership.
Titles, URLs and text are untrusted data rather than execution instructions.
The saved title can also retrieve that actual personal observation through local
lexical recall, without searching unread shared articles.

`STARTED` has no automatic replay after an unknown interruption. Successful local
application marks each item `APPLIED` (a sourced `PERSONAL_RSS` observation) or
`IGNORED` (no personal observation); both exclude the item from future candidates.
A confirmed `FAILED` item is eligible for one later attempt through another
normal opportunity, with a total limit of two. This does not buy an automatic
retry. A saved batch appraisal can retry the local transaction without repeating
selection, reading or appraisal; events, item outcomes and the opportunity result
commit together. Failed/missing/duplicate/unknown appraisal identities cannot
produce fabricated observations. Memory admission remains the existing policy;
this path requests no extra cognition call.

New eligible content is a bounded idle-gate signal derived from candidate IDs,
source generations and content hashes. Fetch/error timestamps and unchanged
refreshes do not wake the model. Gate inspection itself makes no model or network
calls, and it never bypasses the existing due clock/daily ceiling. Read/ignored
items are not a recurring wake source.

### Personal reading decision protocol

An enabled RSS opportunity requires an explicit `choice`: `NO_ACTION`,
`WEB_SEARCH` or `READ_RSS`. The narrow provider alias `action` is normalized
locally to the same choice (including casing), preserving selected item IDs
without another model call. Missing, unknown or conflicting decisions fail
validation rather than becoming silence; `WEB_SEARCH` requires a nonempty query.
The RSS-only prompt names these JSON fields explicitly. Disabled/empty RSS keeps
the original Personal Browse schema and prompt.

Raw provider output and its parsed decision must be distinguished during live
acceptance. A defaulted empty decision is not evidence that a character chose
not to read. Existing completed receipts are not reinterpreted or replayed by
this protocol change; source membership, generations, budgets and item limits
remain server-enforced before actual execution.
