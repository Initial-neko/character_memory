# Avatar Search (P0.15)

Avatar discovery is the first consumer of the search abstraction. This phase intentionally does **not** expose general web browsing to character chat runtime.

## Current flow

1. The Web UI opens the avatar manager from the character header avatar.
2. `POST /v1/characters/{character_id}/avatar/search` calls `SearchProvider.search_images()`.
3. `BraveSearchProvider` uses Brave Image Search with configured country/language/Safe Search.
4. The server returns thumbnail candidates plus opaque `search_id` / `candidate_id` values.
5. The browser selects only those opaque IDs; it never sends an arbitrary download URL to the backend.
6. The backend downloads the cached search result, validates image content type/size, and stores it under `avatar_dir/<character_id>/`.
7. Character profiles expose a versioned local `/avatar/asset` URL; sidebar/header/direct chat/group chat render that local asset.

## Configuration

```yaml
search_provider: "brave"
search_api_key: ""
search_country: "ALL"
search_language: "zh"
search_safe_search: "strict"
avatar_dir: ""
avatar_max_bytes: 8388608
```

`config.yaml` is git-ignored. `config.example.yaml` declares the fields. `BRAVE_SEARCH_API_KEY` may optionally override `search_api_key` for deployment secret injection.

## Reserved boundaries

`SearchProvider.search_web()` and `WebFetcher.fetch()` are deliberately present but raise `NotImplementedError`. A later phase can implement `web_search` and `web_fetch` without coupling those capabilities to avatar storage or the character reaction loop.

Search/tool output must remain external context by default; it should not automatically become persistent character memory.
