# Draft: RSS → Character World Perception (V1)

> **Design / acceptance draft only.** No production implementation in this PR. Do not merge until the follow-up implementation has passed the acceptance matrix and the product boundary is approved.

## 1. Goal and scope

Let a Character **selectively read RSS items already stored locally**, form its own appraisal, and optionally write one recent `WORLD_OBSERVATION`. Keep RSS collection and user reading independent of Character cognition.

Must remain true:
- `RSS source -> RSS item` is deterministic collection, not model output.
- User-facing `/sources` and feed APIs work exactly as they do today.
- Character does not receive RSS as an unrestricted Tool or AgentLoop.
- Observation does **not** imply publishing to Space, sending Direct, adding Memory or creating a Goal.

## 2. Existing integration points (verified on main, 2026-10-08)

| Existing module | Real responsibility | Extension boundary |
| --- | --- | --- |
| `rss_sources.py` / `RssRepository` | SQLite RSS source/item state and feed queries | Add a **read-only Character candidate query**; do not reuse the user-view `period=today` as a perception filter. |
| `rss_runtime.py` | RSS worker/HTTP composition | Keep RSS collection independent. RSS-only collection must never require an LLM or `PersonRuntime`. |
| `world_activity.py` / `WorldActivityScheduler` | Durable `BROWSE` opportunities, per-character daily cap, idle gate | Reuse the existing opportunity; no second RSS scheduler or independent paid wake loop. |
| `world_activity.py` / `WorldActivityService.browse_character` | Persona/Memory-context planning, web search/browser, appraisal, WORLD_OBSERVATION | Add a bounded RSS choice alongside existing WEB / NO_ACTION, without expanding every Direct/Group prompt. |
| `world_activity.py` / `PersonalBrowseAppraisal` | `keep + summary + personal_note` | Reuse semantics and provenance; do not automatically admit Memory. |

## 3. Expected sequence

```text
Existing RSS fetcher -> rss_items (already implemented)
                          |
Existing WorldActivity BROWSE opportunity (same clock and daily quota)
  -> cheap candidate query (only enabled, not cancelled sources)
  -> candidate set: max 8 unread, recent items (e.g. last 7 days)
  -> one Character plan: NO_ACTION | READ_RSS(item ids <=2) | WEB_SEARCH
       | NO_ACTION: no further model calls; preserve idle backoff
       | WEB_SEARCH: preserve current Personal Browse behavior
       ` READ_RSS: load bounded local text, no network page scraping
          -> one bounded appraisal call
          -> keep=false: record processed/ignored, no observation
          -> keep=true: write character-local WORLD_OBSERVATION
                     + durable character/item perception record
          -> end opportunity (no chained actions)
```

The `READ_RSS` choice is an **internal behavioral outcome**, not a public Character Tool. Runtime validates returned article IDs against the current candidate set, source enabled/cancelled state, and content bounds; runtime does **not** ask a second model whether the Character genuinely likes the article.

## 4. Storage, freshness and provenance

- `rss_items` is shared source truth. **A newly ingested article is not automatically known to any Character.**
- Keep a durable per-character-per-item perception ledger (`character_id + rss_item_id`, unique), with an outcome such as `OBSERVED / IGNORED`, timestamp, and optional `source_event_id`. Persist together with the observation Event under the same SQLite transaction; retries/restarts cannot duplicate it.
- Candidate query excludes cancelled/disabled subscriptions. Default candidate lookback is **7 days**, ordered deterministically, max **8 titles and excerpts**; proposed bounds are V1 acceptance values, not hardcoded into general user feeds.
- Only selected, appraised items enter a Character observation. Do not mark unselected candidates as read by that Character. Do not copy all RSS items into each Character's log.
- For retained observations, keep existing `EventType.WORLD_OBSERVATION` and `channel=PERSONAL_BROWSE`; add `source_kind=RSS`, exact `rss_item_ids`, `rss_source_ids`, canonical original article `sources` URLs, `published_at` and observation time. `content` is the character's own `personal_note`/supported summary, never raw Feed HTML.
- Distinguish publication time from collected/observed time, reject invented URLs, and treat Feed text as **untrusted external data** (including embedded prompts and instructions).

## 5. Model and cost constraints

- No per-item LLM calls. **At most one selection/planning LLM call and one RSS appraisal LLM call per existing BROWSE opportunity**; `NO_ACTION` and no-candidate cases have no appraisal call.
- Keep the existing daily BROWSE ceiling, jitter, schedule ledger, non-overlap lock and idle backoff. Do not create an RSS-specific recurring LLM wake.
- Extend idle reconsideration to detect *new eligible RSS items*, using a durable item watermark, without treating every scheduler poll or unchanged feed refresh as a new reason to call the model.
- Hard bound contextual text: candidate metadata <= 8 items; chosen full items <= 2; total RSS text fed to appraisal <= 12,000 characters. Oversized items must be truncated and identified as excerpts.
- Default rollout behind `world_rss_perception_enabled=false` until manual evaluation proves non-regression; when disabled, existing Browse behavior must be byte-for-byte/observably equivalent apart from harmless code structure.
- No new Search/Browser/Claude call for RSS reading; no extra RSS network fetch at appraisal time.

## 6. Acceptance criteria (mandatory)

| ID | Scenario / input | Pass condition |
| --- | --- | --- |
| A1 | 4 sources, one enabled, one disabled, one cancelled, one empty | Candidate reader returns only items from the enabled active source; source/UI data is unchanged. |
| A2 | No RSS subscriptions or no eligible new items | Old browse path works as before; no RSS appraisal, no new Character observation, no extra model calls. |
| A3 | Two Characters see the same candidate; A accepts, B declines | Exactly one character-local observation for A; none for B; ledger is independent per character. |
| A4 | Same item offered again, retry, concurrent tick, restart | Already processed item is not re-appraised or re-recorded; Event + ledger write is atomic. |
| A5 | Article contains prompt injection, invalid external URL, or oversized HTML | No instruction execution/tool call; sanitized bounded text only; original provenance remains correct. |
| A6 | RSS arrives while previous plan was `browse=false` | A genuinely new eligible item can reopen an existing opportunity; unchanged RSS data does not trigger repeated planning. |
| A7 | Character chooses WEB_SEARCH or NO_ACTION | Existing web pipeline and quiet backoff remain available; no forced RSS reading, no forced post/message. |
| A8 | Appraisal `keep=true` | `WORLD_OBSERVATION` includes correct item/source IDs, URL, published/observed times, `source_kind=RSS`; next conversation can refer to actually read content. |
| A9 | Appraisal `keep=false`, fetch failure, missing content, or model error | No false observation; deterministic error/ignore outcome; safe retry according to existing opportunity rules. |
| A10 | Runtime and cost regression | Existing BROWSE daily cap, idle budget, scheduler restart behavior and Direct/Group/Space tests remain passing; no extra AgentLoop or Memory writes. |

## 7. Manual walkthrough (before marking implementation ready)

1. Use `/sources` to add a test RSS source and refresh; verify its article is stored once and visible as a user feed card.
2. Enable experimental RSS perception and manually trigger one existing Character Browse opportunity. Inspect candidate IDs and observe the Character choosing READ_RSS or NO_ACTION.
3. On READ_RSS+keep, inspect the World event metadata, source URL and per-character ledger. Ask the Character about it in an ordinary Direct chat; it may refer to the genuine reading, not an unobserved item.
4. Trigger the same run again and restart the service: no duplicate observation and no duplicate processing of the same article.
5. Disable RSS perception: existing web browsing continues. Verify that no Memory, Space post or Direct message was produced by the RSS pipeline itself.

## 8. Out of scope / implementation handoff

- No AgentLoop, Goal, native Character ToolCall, universal Capability Gateway, new user UI page or Android screen.
- No automatic full-article scraping, automatic Space post, autonomous RSS subscriptions, automatic Memory admission, or LLM summarization of the entire RSS inbox.
- This PR is a **design-only Draft**. Implementation should be a separate small PR, using the tests above as the merge gate.

## 9. Open design decisions for implementation review

- Should `READ_RSS` extend `PersonalBrowsePlan`, or use a separate small bounded decision schema within `WorldActivityService`? Prefer no extra LLM call; keep the public Character protocol unchanged.
- Should processed-but-discarded items be eligible for reconsideration after a meaningful change in interests? V1 defaults to no, unless a clear product reason justifies an expiry policy.
- Whether article content is sufficient for appraisal when the Feed only provides a short excerpt; V1 should label this as a limited observation instead of visiting the original URL.

Review constraint: none of these choices may introduce an extra paid loop or bypass provenance/permission checks.
