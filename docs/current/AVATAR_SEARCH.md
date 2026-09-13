# Avatar Search

Avatar discovery is the first consumer of the search abstraction. This phase intentionally does **not** expose general web browsing to character chat runtime.

## Current flow

1. The Web UI opens the avatar manager from the character header avatar.
2. The optional text box is a **preference hint**, not a raw search-engine query.
3. `AvatarIntentPlanner` asks the existing character model to judge what avatar fits the character **right now**, using bounded context:
   - Persona
   - current Mental State
   - at most 8 recent short chat lines
   - optional user preference
4. The planner returns a compact `AvatarSearchIntent` containing:
   - `visual_intent`
   - 1~3 short image-search queries
   - optional mood/style labels
5. `AvatarSearchService` searches the primary query first. It only spends a second/third provider request when the previous query did not produce enough viable candidates.
6. `SearchApiProvider` uses SearchAPI.io Google Images by default. `BraveSearchProvider` remains an optional fallback.
7. The server returns thumbnail candidates plus opaque `search_id` / `candidate_id` values.
8. The browser selects only those opaque IDs; it never sends an arbitrary download URL to the backend.
9. The backend downloads the cached search result, validates image content type/size, and stores it under `avatar_dir/<character_id>/`.
10. Character profiles expose a versioned local `/avatar/asset` URL; sidebar/header/direct chat/group chat render that local asset.

If LLM planning fails, the server falls back to the previous deterministic `name + identity + avatar` style query so avatar search remains usable. That fixed query is now a fallback only, not the normal path.

## Privacy boundary

Persona, Mental State and recent dialogue are used only inside the LLM planning step. The image-search provider receives only the short generated search query. The planner prompt explicitly forbids copying user names, private facts, relationship secrets or chat quotations into search queries.

The avatar plan is ephemeral tool context. It is **not** written to Character Memory and does not become a normal PersonRuntime action.

## Configuration

```yaml
search_provider: "searchapi"
search_api_key: ""
search_country: "jp"
search_language: "zh-cn"
search_safe_search: "strict"
avatar_dir: ""
avatar_max_bytes: 8388608
```

`config.yaml` is git-ignored and is the normal place to put the local API key. `config.example.yaml` declares the available fields.

For SearchAPI.io, `search_safe_search: "strict"` maps to Google Images `safe=active`. `SEARCHAPI_API_KEY` can optionally override `search_api_key` for deployment secret injection.

Brave remains available without changing the avatar service or UI:

```yaml
search_provider: "brave"
search_api_key: ""
search_country: "ALL"
search_language: "zh"
search_safe_search: "strict"
```

When `search_provider: "brave"`, `BRAVE_SEARCH_API_KEY` is the optional environment override.

## Provider boundary

Both providers normalize results into `ImageSearchResult`:

- original image URL
- thumbnail URL
- source page URL
- source domain
- width / height when available

This keeps avatar caching, candidate selection, SSRF controls and local asset persistence provider-independent.

## Reserved boundaries

`SearchProvider.search_web()` and `WebFetcher.fetch()` are deliberately present but raise `NotImplementedError`. A later phase can implement `web_search` and `web_fetch` without coupling those capabilities to avatar storage or the character reaction loop.

Search/tool output must remain external context by default; it should not automatically become persistent character memory.
