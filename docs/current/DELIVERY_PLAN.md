# Delivery & Acceptance Ledger

> Baseline: `main@38d400ac2ff459c851585d0045735c6b22eed636`, 2026-09-29.
>
> This file separates **implemented on main** from **proven on the actual target machine**. CI-green code is not automatically a real-device acceptance result.

## 1. Current convergence status

| Workstream | Implementation | Real acceptance | Notes |
| --- | --- | --- | --- |
| Stage 1 async defects | DONE | Browser regression covered | Durable Space/Encounter replies, Enter send, archived group removal. |
| Character archive/delete/deferred direct chat | DONE | Manual edge checks pending | Exact-name delete, activity counts, generated characters do not automatically enter the private sidebar. |
| Screen-share image quality | DONE | TARGET-MACHINE PENDING | DISPLAY accepted frames retain up to ~1440px long side; cheap change analysis remains 64px. |
| Ensemble Persona V2 | DONE | Persona-quality soak pending | One structured group research call plus deterministic per-character projection; no N extra Persona calls. |
| ASR server streaming | OPTIONAL | Not a release baseline | Paraformer streaming session and WebSocket protocol exist as manual opt-in; canonical setup remains SenseVoice batch. |
| Shared browser AudioWorklet | DONE | Browser regression covered | PCM16 / 16k shared streaming client. |
| Dictation streaming | OPTIONAL | Not a release baseline | Streaming is used only when Media Runtime explicitly advertises it; canonical setup remains batch ASR. |
| Browser Call streaming | OPTIONAL | Not a release baseline | Streaming is opt-in through Media Runtime capability; canonical setup remains batch ASR with the existing call behavior retained. |
| LLM usage observability | DONE | Runtime soak pending | Feature/Purpose, request/logical ratio, chars, token coverage, retry/error/latency. |
| World browse cost gate | DONE | Runtime soak pending | Repeated browse=false planning can be skipped until a new signal/recheck boundary. |
| Space audience cost gate | DONE | Runtime soak pending | Public social ties prioritized; cold posts use at most two exploration audience slots. |
| Periodic screen observation | DONE | TARGET-MACHINE PENDING | Direct DISPLAY only; significant-change + busy + dedup + interval + hourly quota gates. |
| Settings/config contract | DONE | Manual usability pass pending | Supported knobs live in config.example.yaml with help and common/advanced/diagnostic ownership. |

## 2. P0 target-machine acceptance

### 2.1 Streaming ASR

Run on the machine that will actually host Media Runtime. Record provider/model/device and keep the same audio corpus for comparisons.

| Case | Evidence to record | Pass condition |
| --- | --- | --- |
| 30–60s natural Mandarin | reference transcript, final transcript, CER/D/S/I | no material tail/phrase deletion |
| 0.5–1.5s thinking pauses | segment ids + timestamps | pauses do not arbitrarily lose or duplicate content |
| fast / low-volume / mild noise | reference + final | degradation is visible and bounded, no hallucinated conversational reply from noise |
| Chinese + English technical terms | reference + final | substitutions tracked separately; project/character names explicitly included |
| first partial latency | timestamp | measured, not inferred from UI |
| finalization latency | endpoint -> final timestamp | measured and compared with product tolerance |
| reconnect / cancel | WebSocket event log | no duplicate final message |
| speak while character is replying | browser event log | recognized turn enters pendingTurns and is dispatched after reply |
| repeated sessions | CPU/RAM/VRAM at 1/10/50 sessions | no monotonic resource leak |
| configured GPU execution | health + process/device evidence | no silent CPU fallback |

Do not select a final ASR model from one aggregate score. Report deletion rate separately from CER.

### 2.2 Screen-share readability and periodic observation

Use a real 1920×1080 or higher desktop and explicitly share the target window/screen.

| Scene | Check |
| --- | --- |
| VS Code / IDE, 12–14px text | Character can read representative Chinese/English source text from accepted DISPLAY frame. |
| Browser settings/data table | Small labels and values remain legible enough for the intended Vision provider. |
| Terminal | Commands and short error lines remain readable. |
| Static screen for >2 intervals | After the initial eligible observation, unchanged content creates no new periodic Vision call. |
| Significant window/content change | A new observation may be accepted after the minimum interval. |
| User speaking / reply in flight | Periodic observation is skipped rather than competing with the active turn. |
| Stop screen share | No new observation is scheduled after stop. |
| Hourly cap | Accepted observations do not exceed `periodic_visual_observation_max_per_hour`. |
| Chat history | `VISUAL_OBSERVATION` is not rendered as a fake user message. |
| Memory | Screen observation does not create durable Memory/Intent. |

Use Dev LLM Usage Explorer to confirm periodic observations appear under `VISUAL / SCREEN_OBSERVATION_VISION`.

## 3. Cost-soak acceptance

Capture at least one comparable usage window before and after the convergence changes.

Required breakdown:

- `WORLD_BROWSE_PLAN`: requests, logical calls and number of real browse/appraisal runs;
- `SPACE_AUDIENCE`: calls per autonomous post, with warm vs cold audience examples;
- `SPACE_REPLY`: calls per comment thread and automatic-round depth;
- `VISUAL / SCREEN_OBSERVATION_VISION`: accepted calls/hour and silence rate;
- request/logical-call ratio and retry/error hotspots;
- input-character share for the top Feature/Purpose rows.

The objective is not to force characters to be quiet. The objective is to remove **mechanical model calls that do not correspond to a meaningful opportunity**.

## 4. Real Chromium product pass

Run against the real character-stack, not a mocked page:

- Direct Chat: text, image, voice message, streaming call, speak-over-reply, screen share.
- Group: add/remove member, archived member removal, mentions, large-group readability, grouped message rendering.
- Character creation / one-click group: distinct Persona behavior, avatar prepared, voice or explicit setup state, generated members not forced into private sidebar.
- Space: infinite feed, collapsed replies, durable user comment then asynchronous AI continuation, sparse audience behavior.
- Encounter: durable user reply then asynchronous continuation.
- Settings: Common surface remains small; every field has help; restart/hot-apply semantics match the UI.
- Dev: LLM Usage table identifies feature/purpose hotspots and token-coverage gaps.
- Failure recovery: microphone/camera/display denial, SSE reconnect, Media Runtime/TTS failure, generation failure.

## 5. Release exit criteria

A release candidate may be promoted only when:

1. CI is green on the release candidate commit.
2. The ASR target-machine matrix has recorded results rather than unchecked assumptions.
3. Screen readability passes on IDE/browser/terminal examples.
4. Periodic visual observation shows bounded Vision cost in the Dev usage table.
5. Core Direct/Group/Space/Settings/Dev flows pass the real Chromium run.
6. Known failures are either fixed or explicitly recorded with scope/workaround.
7. `STATUS.md` and the owning domain docs match the code being tagged.

Open feature work unrelated to these acceptance gates (for example experimental Space video generation) does not silently redefine the release baseline.

## Character architecture convergence

This workstream extends the existing PersonRuntime; planned rows are not shipped
features. Each implementation PR must add its own fixed-commit acceptance
result. Engineering gates are independent of live-provider and human-quality
judgments, and dependent milestones cannot merge on an unaccepted predecessor.

| Milestone | Scope | Implementation | Independent engineering acceptance | Target-machine / human acceptance |
| --- | --- | --- | --- | --- |
| M0 | Isolated Character cost/call baseline | Merged in #249 | Accepted at 43e0512; CI green | No live-provider or Persona claim |
| M1 | Historical observations, lifecycle and behavior contract | Merged in #250, #251, #252 and #254 | F04/F01/lifecycle and legacy metadata supplement accepted; CI green | Live Persona quality pending |
| M2 | Finite Intent deferral | Merged in #253 | Accepted at a632121; CI green | Live-provider quality pending |
| M3 | Minimal World Web execution/feedback contract | Implemented in #255/#256 | Accepted at 367047d; merged #255/#256; CI green | Live browser/provider quality pending |
| M4 | RSS personal reading through existing World opportunity | Implemented in #257 | Accepted at 6abaded; merged #257; CI green | Live-provider / human quality pending |
| M5 | Integrated compatibility, cost and opt-in rollout | Lifecycle gates implemented in #258; flags remain opt-in | Accepted at bb5a299: 215 regressions and 10 additional probes; CI green | Windows browser/short soak recorded; human accepted unread truthfulness and expression sample only; live post-reading quality, seven-day continuity and formal rollout pending |

The optional World saving switch defaults off and changes only background browse
cadence. Permission, idempotence and failure safety apply in both modes. Optional RSS
personal reading defaults off; shared RSS collection is not personal experience.
Acceptance must use its own configuration, database and process ownership;
formal service startup and restoring an acceptance database over formal data
are not part of automated milestone validation.
