# Release Readiness Audit

This is a living pre-release audit for Character Memory. It is not an implementation task ledger and it does not replace source code, tests, or the release policy. Update it before each release candidate/stable promotion, then keep only conclusions that still describe the current release surface.

Last audited:

- Date: 2026-09-22
- Target release window: 2026-09-23
- Audited baseline: `main@f80419f17c33e4a30cce5851e4f7b23206f45449`
- Package version on baseline: `0.5.0rc1`
- Release principle for the next window: **feature freeze; correctness, compatibility, configuration clarity, documentation and validation first**

The source tree and tests remain authoritative. Open PRs are listed here only because they directly affect the next release decision.

---

## 1. Executive conclusion

The project has enough product surface for another release, but the main risk is no longer “missing features”. The risk is **surface drift**:

- recently merged behavior is not fully reflected in release notes;
- several feature branches are ready enough to look mergeable but should not be pulled into a release freeze;
- some runtime/configuration behavior is duplicated between Settings and Dev surfaces;
- a few product commitments exist in code branches or current product direction but are not yet complete on `main`;
- several subsystems are now large enough that ownership is becoming a maintenance problem, even though they are not release blockers by themselves.

For the 2026-09-23 release window, the recommended default is:

> **Do not add another major capability. Close correctness/configuration gaps, validate the shipped core, update version/release notes, and defer feature PRs.**

If normal-stack soak is not completed before the release, prefer another release candidate (for example `0.5.0-rc.2`) rather than promoting directly to a stable `0.5.0`. The repository release policy already requires real local soak in addition to CI for stable promotion.

---

## 2. Release gate

### P0 — must be resolved before publishing

#### 2.1 CHANGELOG / version / tag must describe the actual main branch

Current `CHANGELOG.md` says:

`## Unreleased — No queued release changes yet.`

That is already stale relative to current `main`. At minimum, the following merged behavior is newer than the recorded `0.5.0-rc.1` baseline and must be accounted for before the next release:

- bounded two-level Character Space threaded replies and reply propagation from PR #92;
- Character Space media-prompt behavior and Dev Console layout cleanup from PR #95.

Before release:

1. decide the next version;
2. update `pyproject.toml`;
3. move the actual shipped changes into `CHANGELOG.md`;
4. verify the API reports the same installed package version;
5. create the immutable tag/GitHub Release only after final validation.

This is a release blocker because a release whose changelog/version does not match its executable baseline is not a trustworthy acceptance point.

#### 2.2 Final main must be CI-green after the last accepted fix

Do not rely on a PR CI result after more changes have landed on `main`.

Final release commit must pass:

- Linux full pytest;
- Windows-sensitive smoke;
- Browser/Chromium smoke.

If a release fix is merged after the last green run, rerun the final gate.

#### 2.3 Normal-stack local soak is required for stable promotion

CI intentionally does not validate real local models, CUDA, external search providers, browser/microphone behavior, or a developer's existing database.

The final local soak should cover at least:

- startup against an existing database and schema migrations;
- Direct text / sticker / image / voice message;
- Group text / mention / image / voice message;
- a medium/large Group (not only two members);
- one-prompt Ensemble group creation and confirmation;
- Character Space older-post pagination, image lightbox, comments/thread replies and one media post;
- Random Encounter GENERATED and, when search credentials exist, WEB path;
- archive / restore behavior;
- Settings persistence and hot-apply paths;
- formal `:8001/v1/tts` through the actually selected provider;
- restart persistence after configuration changes.

#### 2.4 Freeze new product capability PRs

Until the release is published, the default merge decision for new capability work should be **defer**.

The current open PR set already demonstrates why this matters: multiple independently valid feature branches touch the same Space, Voice, Dev and runtime surfaces. A final-day feature merge creates more integration risk than product value.

---

## 3. Open PR triage for the next release

### PR #93 — World Pulse + independent personal browsing

**Decision: defer until after the release.**

Why:

- it is a large feature PR (~1.9k additions);
- it adds another scheduler/background activity domain immediately before a release;
- current changed files expose World Pulse primarily through backend/Dev diagnostics; there is no normal end-user Pulse surface in the main Chat/Space UI;
- the current technical-debt register already identifies background-worker ownership as a medium-priority structural problem.

This is useful work, but it is not a release hardening change.

Before it becomes a normal product capability, decide what the user-facing Pulse surface actually is. A Dev-only implementation should not be advertised as a completed user feature.

### PR #94 — independent microphone and screen/camera sharing

**Decision: defer until after the release.**

The PR is CI-green and addresses a valid product requirement, but its own scope is still a user-visible call capability change. It also explicitly states that it was not intended for the current release work.

Current `main` therefore still has the old call-session coupling semantics. Do not claim fully independent microphone/screen lifecycle in release notes until this lands.

### PR #96 — Dev Console ownership + archived voice lifecycle

**Decision: release-hardening candidate.**

This PR addresses existing behavior rather than adding a new product mode:

- Dev Space/Group test tuning stops silently persisting formal configuration;
- Random Encounter formal settings move into Settings Center;
- obsolete Dev TTS UI is removed while ASR/Media Smoke remain diagnostics;
- archived characters stop participating in active voice registry/synthesis and proactive wake;
- archive/restore keeps `voice.yaml` but refreshes active GSV registration.

CI on its current head is green.

However, if #96 is accepted for the release, its documentation must be updated at the same time. In particular:

- `docs/current/DEV_CONSOLE.md` currently says Space Dev controls persist configuration;
- `docs/current/SETTINGS_CENTER.md` currently says Dev can persist the same Space values;
- `README.md` currently describes Dev as a combined ASR/TTS test surface.

Merging behavior without updating these maintained contract documents would create another release-time drift.

### PR #97 — always-visible voice-message text

**Decision: do not merge as-is. Treat as a possible bug-fix candidate only after clean rebase and full CI.**

The product expectation is valid: durable voice messages should show their canonical spoken text directly instead of requiring a “文本/翻译” reveal interaction.

Current `main` still hides:

- Direct/Group voice-message text through the voice bubble renderer;
- Space voice transcript behind a `文本` button.

But PR #97 is currently diverged/merge-conflicted relative to `main` and has no current CI run on its head. It also contains stacked Space changes that overlap with already-merged PR #92.

For the next release:

- either rebase it down to the minimal transcript-visibility fix, run full CI and accept it as a bug fix;
- or explicitly defer it and document the current behavior as a known UX limitation.

Do not merge the current dirty branch merely because the desired behavior is small.

---

## 4. Current product capability matrix

| Area | Current main | User-visible | Config status | Release assessment |
| --- | --- | --- | --- | --- |
| Direct Chat | Implemented | Yes | Core model/runtime settings available | Core; validate |
| Group Chat | Implemented, up to 12 members | Yes | Membership is product state, not a global setting | Core; validate cost/latency |
| Async accept + SSE | Implemented | Yes | Not intended as a user toggle | Core; single-process only |
| Memory Inspector/governance | Implemented | Yes, Runtime drawer | No extra policy UI beyond pin/forget/restore/correct | Core; advanced governance deferred |
| One-prompt Ensemble group | Implemented | Yes | No special global config | Core; validate real web/model path |
| Character Space | Implemented | Yes | Space behavior configurable | Core; threaded replies are very recent |
| Space image lightbox/pagination | Implemented | Yes | Not a setting | Core |
| Space voice/image media | Implemented | Yes | Provider/gates configurable | Core; real-provider soak |
| Random Encounter | Implemented | Yes | Formal Encounter settings missing from Settings on current main; #96 fixes | Fix config ownership before claiming complete |
| Voice Message | Durable Direct/Group flow implemented | Yes | TTS provider in Settings | Canonical text visibility still unresolved (#97) |
| Voice Call | Implemented | Yes | Silence/VAD controls partly configurable | Mic/screen independence is not on main (#94) |
| Avatar search/generate | Implemented | Yes | Provider config exists | Core |
| Character voice templates/GSV | Implemented | Yes | Settings + voice drawer | Archive semantics incomplete on main; #96 fixes |
| World Observation | Implemented inside Space/diagnostics | Indirectly | Search/browser settings available | Core supporting capability |
| World Pulse / Personal Browse | Not on main | No normal user Pulse UI | PR #93 config only | Defer |
| Qwen3 VoiceDesign | Experimental tool | Workbench only | Sidecar/tool config | Do not advertise as formal chat TTS |
| CosyVoice | Optional Workbench/sidecar support | Workbench only | Optional | Not part of formal provider contract |
| Streamlit Inspector / life simulation CLI | Supported developer/research code | Developer-only | CLI/config | Keep supported; not product UI |

---

## 5. Promised or expected behavior that is not fully closed

These are not all release blockers. They are important because the project should not accidentally advertise them as finished.

### 5.1 New character / Ensemble creation does not fully provision avatar + voice

The canonical `ApiCharacterService.create_from_draft()` path creates the Persona and registers the runtime. It does **not** guarantee:

- a first avatar;
- a selected voice template.

This means “one-prompt group creation” is functionally complete as a group/persona flow, but not yet a fully provisioned character-pack flow.

Product expectation for the current direction is stronger: newly created characters should arrive with at least a usable avatar and a voice choice when possible.

Release decision:

- do not add this provisioning pipeline tomorrow;
- do not describe Ensemble creation as “fully prepares every character's media identity”;
- track avatar/voice provisioning as post-release closure work.

### 5.2 Voice call mic/screen independence

Implemented only in PR #94, not on main.

Current release should not claim that microphone can always be turned off while screen/camera sharing remains active.

### 5.3 Voice-message canonical text is still hidden on main

PR #97 exists but is not merge-ready.

Current maintained Voice docs also still describe a translation/browser slot. The release must either land the minimal fix or describe the current reveal behavior accurately.

### 5.4 World Pulse / higher-frequency personal browsing

Implemented in PR #93, not on main.

More importantly, the current PR mostly provides durable backend data and Dev controls; it does not yet close the normal end-user Pulse presentation loop. Treat it as post-release feature work.

### 5.5 Encounter visual identity

Encounter V1 can create/trial/accept a candidate, but the candidate presentation does not guarantee a generated avatar before acceptance. This is consistent with the broader “character creation provisioning” gap above.

---

## 6. Backend implemented but user surface is incomplete or intentionally absent

Not every backend endpoint needs a button. The important distinction is whether the absence is intentional.

### Intentional diagnostic/research surfaces

These are not user-facing omissions:

- `/v1/state/{character_id}`;
- `/v1/simulate`;
- World Browser diagnostic routes;
- Dev scheduler/status routes;
- CLI day/simulate/tick/inspect;
- Streamlit Inspector.

They should remain documented as developer/research tools, not normal product features.

### Real user-surface gaps

#### World Pulse

PR #93 creates significant backend/state/scheduler behavior but no normal user-facing Pulse page/card/feed. This is the clearest example of “implemented logic without a completed product surface”.

#### Dedicated Space post detail page

`GET /v1/space/posts/{post_id}` exists, but the maintained Space document explicitly says a separate full post-detail page is not implemented. The inline feed/thread experience is the current product contract.

This is not a release blocker.

#### Space live push

Space still has no full push/SSE feed-update contract. Direct/Group are live through SSE; Space relies on feed loading/refresh behavior.

Do not silently imply that Space has the same live-delivery semantics as chat.

---

## 7. Configuration audit

### 7.1 Concrete configuration bug: Dev and Settings overlap

On current `main`, Dev Space/Group config writes formal configuration and hot-applies it.

That makes a temporary soak experiment such as “10 min” indistinguishable from a permanent application setting unless the operator remembers to undo it.

PR #96 correctly simplifies the ownership:

- Settings Center = persisted formal configuration;
- Dev Console = runtime-only experiment + diagnostics.

This is a good simplification and should be preferred over adding more duplicated controls.

### 7.2 Random Encounter settings exist in `Settings` but are not exposed in Settings Center

Current config fields:

- `encounter_enabled`
- `encounter_interval_minutes`
- `encounter_web_probability`
- `encounter_poll_seconds`
- `encounter_max_pending`

Current main does not expose them in the Settings schema. PR #96 closes this.

This is an actual configuration-surface omission because Encounter is already a normal user-visible feature.

### 7.3 Legacy Space daily-window fields are loadable but unused

`space_daily_window_start_hour` and `space_daily_window_end_hour` remain in `Settings` only for compatibility. The interval scheduler no longer consumes them.

These fields are true deprecation candidates.

Recommended lifecycle:

1. keep load compatibility through the next release;
2. mark them deprecated in config/migration documentation;
3. remove them only in a version where old config migration is explicit.

Do not add them to Settings UI.

### 7.4 File-size safety limits can remain config-only

`media_max_bytes` and `avatar_max_bytes` are not currently exposed in Settings Center.

That is acceptable. They are safety/transport limits, not routine user controls. Making every guard configurable would increase support surface without meaningful product value.

### 7.5 Hard product/safety caps should not automatically become settings

Examples:

- active character hard ceiling 20;
- Group member ceiling 12;
- Space audience ceiling 10;
- bounded autonomous/thread reply limits.

These are cost/correctness boundaries. Keep them code-level unless a real product need requires configuration. “Everything configurable” would be a regression here.

---

## 8. Complexity audit

File size alone is not technical debt, but the current hotspots now correlate with multiple responsibilities.

### 8.1 `tts_lab.py` — ~1,279 lines

Currently owns:

- formal Provider adapters;
- Workbench audition;
- Kokoro/Edge/GSV/CosyVoice integration;
- Qwen VoiceDesign proxy;
- VoiceDesign artifacts/freeze;
- GSV voice reload;
- FastAPI routes.

This is the clearest post-release split candidate.

Recommended direction:

- keep formal Provider runtime adapters in one runtime module;
- move Workbench/VoiceDesign orchestration into separate tooling modules;
- retain the stable browser contract through `:8001/v1/tts`.

Do **not** perform this split on release day.

### 8.2 `space_autonomy.py` — ~1,038 lines

Currently owns:

- context/world exploration;
- Space opportunity planning;
- audience selection;
- comment-thread character propagation;
- scheduler;
- observability/memory metrics.

This module grew because several valid Space features converged in one place.

Post-release split should follow behavior ownership rather than arbitrary line counts, for example:

- opportunity/world planning;
- social interaction/audience/thread propagation;
- scheduler/status.

The existing `space_media_executor.py` is already a good example of separating a concrete responsibility.

### 8.3 `group_conversation_service.py` — ~787 lines

It combines:

- user fact construction/mention resolution;
- member context/reaction orchestration;
- normal group reaction;
- autonomous group reaction;
- send/commit/watermark semantics.

The recent shared Direct/Group reaction/materialization work reduced cross-channel duplication. Do not undo that.

A future split should probably isolate **Group policy/orchestration** from **autonomous-group policy**, while continuing to reuse the shared reaction/materialization layers.

### 8.4 `storage/sqlite.py` — ~740 lines

It mixes:

- schema creation/migrations;
- core Event/Memory/Intent CRUD;
- trace and mental-state storage.

The first safe extraction is migrations/schema setup, not a large repository rewrite. Moving every table into a new abstraction before a release would add churn without changing correctness.

### 8.5 Browser core modules

- `web/app.js` ~603 lines;
- `web/dev.js` ~748 lines;
- `web/space.js` and `web/voice.js` are also large.

Concrete extraction opportunities:

- Memory Inspector can leave `app.js`;
- character navigation/capacity drawer can leave `app.js`;
- Dev cards can own their own small controller modules.

Do not replace the current browser code with a new framework merely to reduce file size.

---

## 9. Abstraction audit

### Abstractions that are already paying off — keep them

The following recent abstractions reduce real drift and should be protected:

- shared Direct/Group reaction engine;
- shared expressive Action -> Message materialization;
- shared incoming user-fact normalization;
- shared message projection;
- shared browser message-content rendering;
- `PersonContextBuilder`;
- typed `CoreApiRouteAccess`;
- centralized `RuntimeServices`;
- formal TTS provider registry.

These solve concrete previously-observed divergence.

### Next useful abstraction: background-service ownership

Current Character Runtime owns multiple independent loops/workers:

- reaction/SSE delivery;
- proactive wake;
- Space scheduler;
- Group autonomy scheduler;
- Encounter scheduler;
- asynchronous visual/voice materialization;
- future World Activity if PR #93 lands.

The next structural abstraction should be one typed lifecycle owner with:

- start;
- stop;
- health/status;
- deterministic shutdown order.

This does **not** require Redis, Celery or a distributed scheduler.

It should happen after the release, preferably before another major autonomous scheduler is merged.

### Next useful abstraction: sidecar support

Media/GSV/Qwen runtimes duplicate some WAV/CUDA/process-support behavior.

A small shared sidecar-support module is reasonable once another sidecar change requires touching the same logic twice.

### Abstractions to avoid for now

Do not preemptively create:

- one universal action schema for Direct/Group/Space/World;
- a generic relationship/social graph;
- distributed queue infrastructure;
- a generic “scheduler base class” with many hooks;
- a frontend framework migration;
- a vector database purely because the current SQLite file is large.

The current channel-specific decision schemas are intentional and should remain until long-run evals demonstrate semantic drift.

---

## 10. Simplification opportunities

### 10.1 Release freeze itself is the largest simplification

For the next release, the highest-value simplification is to stop expanding the supported surface.

Merge only:

- correctness fixes;
- configuration ownership fixes;
- compatibility fixes;
- documentation/version corrections;
- small UI fixes with clear regression coverage.

### 10.2 Dev Console should be diagnostics, not a second Settings Center

PR #96's direction is correct.

The Dev Console should own:

- health;
- status;
- manual triggers;
- runtime-only experiment overrides;
- smoke tests;
- raw diagnostic payloads.

Settings Center should own persisted application choices.

### 10.3 Keep experimental providers clearly outside the formal contract

Qwen3 VoiceDesign and optional CosyVoice should remain Workbench/experimental tools unless they are deliberately promoted into the formal `:8001/v1/tts` provider contract.

This reduces the number of combinations that release validation must promise.

### 10.4 Old branches should be cleaned after release

Several historical branches are hundreds of commits behind main, including old config/release-hardening/document-reorganization work.

Do not delete branches during the final release window if they may still contain useful archaeology. After the release, close/delete clearly superseded branches so “branch exists” no longer looks like “feature still waiting to ship”.

---

## 11. Errors, omissions and release-process drift found in this audit

### 11.1 CHANGELOG Unreleased is stale

Release blocker. See §2.1.

### 11.2 `claude.readme` is stale

It still records an old main baseline and old product facts, including the earlier 10-character hard-cap interpretation.

This file is an acceptance bridge, not the product source of truth, but stale acceptance text is dangerous during parallel development.

Before/after the next release, either:

- refresh it to the current slice;
- or reset the “current baseline/current slice” section rather than allowing it to accumulate old state.

### 11.3 PR #92's historical PR body conflicts with what happened

The PR body says it was intentionally not to be merged during current release work, but it is now merged into main.

The code is CI-green, so this is not a code defect. It is a release-process signal: merge intent can drift while many parallel branches are active.

For the final 24-hour release window, use an explicit freeze rule rather than relying on old PR descriptions.

### 11.4 PR #96 changes maintained behavior without maintained-doc updates

If merged as-is, Dev/Settings docs become incorrect.

Fix docs in the same release-hardening sequence.

### 11.5 PR #97 is not currently merge-ready

It is dirty/conflicted and has no current CI on its head.

Do not treat “small UX fix” as evidence that the branch is safe.

### 11.6 Current config comment still describes an old 10-slot Encounter limit

The current main `config.py` comment says accepting a candidate consumes one of “10 active slots”, while the actual contract is soft warning at 10 and hard stop at 20.

This is low runtime risk but should be corrected with the next documentation/config cleanup.

### 11.7 Dependency reproducibility is still incomplete

The technical-debt register records that the repository does not publish a verified `uv.lock`.

For another RC this can remain explicit debt if the environment is reproducible enough in practice.

For a stable release, strongly prefer generating/verifying/committing the lock and tightening CI to `--locked`, or explicitly document the accepted reproducibility risk.

### 11.8 Single-process runtime is a real boundary

SSE/reaction/background scheduling is process-local. SQLite durability does not make multiple Character Runtime workers safe.

Release/run documentation should continue to assume one Character Runtime process.

---

## 12. Known deferred / non-release capabilities

These should be kept out of the next release promise unless they are deliberately finished and validated:

- World Pulse / personal browsing from PR #93;
- independent mic/screen call lifecycle from PR #94;
- advanced Memory governance (bulk rules, knowledge graph, freshness/confidence policy);
- Space push/SSE feed updates;
- separate full Space post-detail page;
- image/voice attachments inside Space comments;
- background autonomous Group `GENERATE_IMAGE`;
- full-duplex WebRTC call transport;
- barge-in / interrupting TTS;
- streaming ASR partials;
- streaming/chunked TTS;
- character-initiated calls;
- relationship graph / society scoring;
- distributed scheduler / Redis/Celery;
- multi-process Character Runtime;
- automatic stale-world-fact invalidation;
- ANN/vector-database migration without benchmark evidence.

Qwen3 VoiceDesign and optional CosyVoice are tools/experiments, not formal realtime chat provider promises.

---

## 13. Tomorrow's recommended work order

### Before accepting more code

1. Freeze #93 and #94.
2. Decide whether #96 is part of the release; if yes, update maintained docs with it.
3. Decide whether #97 is worth a minimal clean rebase; otherwise explicitly defer.
4. No new feature PRs.

### Release hardening

1. update `CHANGELOG.md`;
2. decide next version (`rc2` vs stable);
3. update package version;
4. update stale maintained docs/acceptance bridge;
5. run full CI on the final commit;
6. run normal-stack local soak;
7. verify existing database migration/startup;
8. verify restart persistence and provider health;
9. tag/release only after all accepted fixes are on the tested commit.

### If time is limited

Prioritize in this order:

```text
data loss / duplicate durable fact / migration / startup
> wrong character state or wrong memory/person identity
> message delivery / SSE / voice materialization correctness
> archive lifecycle correctness
> configuration silently doing the wrong thing
> major UI path unreachable
> documentation/version drift
> cosmetic UI defects
> refactors
> new features
```

---

## 14. Focused local acceptance matrix

Use a small fixed matrix rather than exploratory clicking only.

### Direct

- text reply;
- silence/no-action path;
- sticker;
- image input + Vision;
- generated image;
- durable voice message;
- Wake;
- Memory Inspector pin/forget/restore/correct.

### Group

- 3-person ordinary group;
- one larger group (6-8 members is enough to expose latency/cost behavior);
- @ mention;
- sticker/image/voice message;
- user-triggered Group ImageGen;
- autonomous Group opportunity;
- Group archive/restore;
- user message arriving while autonomous work is generating.

### Character creation

- ordinary character creation;
- 10 -> 11 confirmation;
- 20 hard stop;
- archive one character and restore;
- one-prompt Ensemble creation;
- reuse an existing member instead of duplicating it;
- note explicitly that avatar + voice provisioning is not yet automatic.

### Character Space

- first page + older-page pagination;
- image lightbox;
- user comment;
- reply to a reply;
- bounded automatic character thread;
- sticker comment;
- image media;
- voice media;
- World Observation;
- archived character cannot create new activity.

### Encounter

- GENERATED candidate;
- WEB candidate when search/browser credentials exist;
- temporary chat;
- keep;
- dismiss;
- 10+ active-character confirmation behavior;
- restart-safe pending candidate state.

### Voice / media

- ASR;
- formal TTS through `:8001/v1/tts`;
- configured Kokoro/Edge/GSV/Sherpa provider as applicable locally;
- one durable voice message survives refresh/restart;
- GSV voice-template assignment;
- if #96 lands: archived character cannot synthesize by character id, restore re-enables it.

### Operations

- `:8000` Character Runtime health;
- `:8001` Media Runtime health;
- `:8002` Dev diagnostics;
- `:8003` Settings persistence;
- `:9002` TTS Provider Lab;
- restart stack and verify values/state remain correct.

---

## 15. Post-release engineering sequence

Do not execute this as part of the release freeze. This is the recommended order for the next development window.

1. **Background-service lifecycle owner** before adding another autonomous scheduler.
2. **Character creation provisioning closure**: avatar + voice assignment for normal and Ensemble-created characters.
3. **Voice/call UX closure**: reconcile #94/#97 cleanly onto main.
4. **World activity product decision**: decide the actual user-facing Pulse surface before merging a large Dev-only backend.
5. **Split `tts_lab.py` responsibility** if VoiceDesign/provider changes continue.
6. **Split Space orchestration** if threaded/audience/world changes keep converging in one module.
7. **Extract SQLite migration ownership** before a larger persistence refactor.
8. **Dependency lock** before treating the line as a long-lived stable support baseline.
9. Only then reconsider larger research work such as relationship models, ANN memory indexing, distributed schedulers or additional provider families.
