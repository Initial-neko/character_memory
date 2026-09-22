# V1 Technical Debt Register

This register records only debt that still exists on current `main`. A possible refactor is not automatically a required refactor.

## Recently resolved

### Route-order service locator coupling

SearchProvider, AvatarStore/AvatarSearchService, ImageGenerationProviders and World Browser/Observer are now constructed once by `RuntimeServices` at Character Runtime composition time. Avatar/Visual/World routes consume those services instead of creating infrastructure and publishing it into `app.state` for later routes to discover.

Production behavior no longer depends on `attach_avatar_routes()` running before World/Visual/Space. Typed `CharacterRuntimeAccess` replaces the former core `SimpleNamespace`. Compatibility properties such as `avatar_store` / `image_generation_providers` delegate to `RuntimeServices`; feature modules may still attach process-local handles such as schedulers/hubs until lifecycle consolidation happens.


### TTS provider/config drift

Formal realtime Provider metadata now lives in `character_memory.tts_registry`. Config validation, Settings and Media routing consume the same Provider ids/default metadata instead of maintaining independent hard-coded lists.

The obsolete Qwen3 0.6B realtime Workbench adapter was removed. Qwen3 remains only as manual experiment / VoiceDesign tooling.

### Settings runtime-state ambiguity

Settings now separates durable persistence from runtime application. A GSV reload failure after a successful save is reported as:

```text
persisted = true
runtime_apply.applied = false
```

rather than falsely reporting that persistence failed.

Project `.env` is no longer copied into every child process, so a persisted value cannot masquerade as a higher-precedence system environment override and hide later Settings edits.

### TTS device semantics

Provider/Voice/Speed remain hot. GSV can hot-reconfigure device. Kokoro and Sherpa device changes are explicitly restart-required for the owning TTS runtime only (`:9002` / `:8001`), not for the whole stack.

### Sherpa Workbench routing

Sherpa audition now uses `:8001/v1/providers/sherpa/tts`, which bypasses the formal chat selector. The Workbench can no longer display Sherpa while accidentally synthesizing the configured GSV/Kokoro/Edge Provider.

### Embedding startup/network ownership

The local sentence-transformers runtime is strict-offline and Web startup eagerly warms the runtime in the background. Setup explicitly prefetches the embedding model; normal startup/first chat cannot silently download from the Hub.

### Long-memory unbounded Python scan

Recall/admission now uses a bounded recent + high-importance vector candidate working set. Exact duplicate detection still queries all active Memory text through SQLite. This postpones the need for a vector database without making per-turn NumPy work grow without bound.

### Legacy trace compatibility full scan

The post-migration compatibility path is retained, because old code may have written legacy ACTION trace metadata after migration 004. It now tracks an incremental event-id cursor instead of rescanning every historical ACTION row at every process start.

### Settings/cross-service test visibility

CI now installs the canonical `all` runtime dependency set for the main test job, has a browser Settings Provider/Voice/Device flow, and has a contract proving Sherpa audition bypasses the formal selector.

### GSV unload observability

Settings no longer swallows every GSV unload exception. Runtime application failures are surfaced through the same persisted-vs-runtime result.

### `.pytest-tmp/`

The throwaway harness directory is ignored.

## High priority / semantic architecture

### Space / World cognition path divergence

Direct/Group cognition runs through PersonRuntime Context/Recall/validation. Space post planning and World explore/appraisal currently reuse the same Persona/Memory/Mental State data but still compile part of their own context and call the model outside the main PersonRuntime cognition pipeline.

That is not yet a second persisted Persona, but it is a real semantic drift risk: future Recall/relationship/context changes could affect chat and public Space behavior differently.

Do **not** solve this by blindly routing every web observation into normal chat actions. The next design needs a shared cognition/context layer plus explicit channel policy. This work is intentionally deferred until Memory governance and World-memory semantics are discussed.

### Memory governance / intervention

Automatic Memory Candidate + admission exists, but there is no complete user/developer control plane for inspecting why a Memory was admitted, correcting/removing/pinning it, or preventing selected source classes from becoming long-term memory.

World Observation makes this more important because external information has provenance, freshness and trust semantics that ordinary relationship memories may not have. Raw web text is already blocked from direct Memory writes; the policy for appraisal summaries remains an open product decision.

## High priority / environment reproducibility

### Dependency lock

The repository does not currently publish a verified `uv.lock` from this branch. Do not hand-write one.

`scripts/sync-all.sh` now behaves safely in both cases:

```text
uv.lock present  -> uv sync --extra all --locked
no uv.lock       -> uv sync --extra all
```

A developer-generated, verified local lockfile can therefore be added later without changing the setup contract. Once it is intentionally committed, CI should also be tightened to require the lock rather than merely support it.

## Medium priority / runtime lifecycle

### Background worker ownership

Character Runtime currently owns several independent process-local loops/workers: ReactionScheduler/SSE, proactive intent polling, Character Wake, Space Autonomy and asynchronous visual/voice work. They are correct enough as single-process components, but start/stop ordering is spread across API and route modules; some shutdown hooks explicitly manipulate ordering.

Before adding many more autonomous schedulers, introduce one typed background-service/lifespan owner with start/stop/health semantics. This does not require Redis/Celery or a distributed queue.

## Medium priority / observe before refactoring

### Deprecated browser audio capture API

`web/dictation.js` still uses `AudioContext.createScriptProcessor()`. It works today but is deprecated by browsers. A future migration to `AudioWorklet` needs real microphone/browser regression coverage and must preserve the current PCM16/dictation contract.

### Duplicated sidecar infrastructure

Audio/WAV and CUDA support helpers still have copies across the media/GSV/Qwen experimental runtimes. Because GSV and Qwen run in different virtual environments but import the same Character Memory source tree, a small shared sidecar-support module is feasible.

Do not refactor solely for deduplication; do it when another sidecar change would otherwise require modifying the same lifecycle logic in multiple places.

### Source-string tests in `test_dev_stack.py`

Some launcher tests still assert source substrings. Behavioral probe tests exist, but spawn/environment ownership could be exercised more directly with subprocess fakes. This is test maintainability debt, not a current runtime defect.

### Large edge modules

Several modules are large (`api.py`, group service, SQLite store, visual routes, and major web JS files). Size alone is not a reason to move code. Split only where ownership/testing conflicts become concrete.

### TTS Workbench / Provider Runtime coupling

`:9002` currently owns both formal adapters and the Workbench (including optional VoiceDesign UI orchestration). The stable browser-facing API remains `:8001/v1/tts`, so this internal coupling is acceptable while lifecycle needs are aligned.

If Workbench-only tooling begins forcing production Provider lifecycle/dependencies, split the UI/tool orchestration from formal Provider Runtime in a deliberate versioned change.

## Longer-term research debt

### Memory indexing strategy

The bounded candidate set makes current complexity predictable, but it is not an ANN index and does not solve every future scale case. Measure real Memory count/latency before selecting sqlite-vec, FAISS, another ANN layer, or a dedicated vector store.

### Multi-process application runtime

ReactionScheduler/SSE delivery remains process-local. SQLite is durable, but multiple Character Runtime workers would need a durable/shared job queue and cross-process event transport before being considered correct.

### Life / Inspector and historical CSS

The frozen life simulation/inspector and milestone-named CSS remain supported code, not dead code. Removal/renaming would create broad churn and belongs to a larger version if product direction warrants it.

## Priority rule

```text
real correctness risk
> reproducibility/platform stability
> semantic drift
> frequently edited ownership
> readability
> cosmetic directory/name cleanup
```

Any broad refactor should first identify the real failure mode it removes.
