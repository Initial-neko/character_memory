# Character Tool Calling — Architecture Proposal (Draft)

> Status: **DRAFT / NOT IMPLEMENTED**. This is a design exploration for a future implementation PR, not a description of current behavior or a commitment to enable any tool.
>
> Baseline inspected: `main@cb5e8e0` (2026-10-01). Source and tests remain authoritative. In particular, [PERSON_RUNTIME](../current/PERSON_RUNTIME.md), [VISUAL](../current/VISUAL.md), [SOCIAL_WORLD](../current/SOCIAL_WORLD.md), and [EVALS](../current/EVALS.md) describe **current** behavior. Do not update them to describe proposed behavior until implementation lands.

## 1. Motivation and scope

Characters should be able to decide to use already supplied tools during existing natural activity opportunities, without a new per-character Agent or a separate tool scheduler. Example: a character voluntarily requests an avatar update. A longer-term goal is *goal-directed multi-tool composition*, but **phase one only evaluates at most one tool invocation per opportunity**.

The main architectural issue is **parameter ownership**: asking a model to supply all provider/API-specific parameters would expose unstable infrastructure details, multiply prompt instructions, increase validation/retry cost, and make model outputs harder to maintain.

Proposed approach:

- Prefer **native OpenAI-compatible Function/Tool Calling** for new tools, subject to actual provider compatibility tests. Keep the existing structured `PersonReaction` protocol for cognition/expression; do not convert its fields wholesale to tools.
- Reuse the existing `PersonRuntime`, `PersonContextBuilder`, schedulers, `RuntimeServices`, media services, and durable SQLite boundaries. A minimal registry/adapter is sufficient; **no new autonomous Agent runtime**.
- Built-in, low-risk, configured tools should be *available by default* to eligible characters. Availability is not compulsory invocation; characters can refrain. Runtime/channel policy, rate limits, and side-effect authorization are enforced separately.
- Do not implement any of this in this Draft PR. The contract, migration strategy, defaults, and exact first tool remain subject to review and future tests.

## 2. Verified current boundaries (not future claims)

| Area | Current behavior on baseline | Source |
| --- | --- | --- |
| Provider chat | Structured calls request `response_format={"type":"json_object"}`, read `choices[0].message.content`, and validate the Pydantic schema. The generic `tools` / `tool_calls` response path is not implemented here. | `src/character_memory/llm/client.py` |
| Person reaction | `PersonReaction.actions[0..3]` represents outward actions and the current internal `GENERATE_IMAGE` intent; cognition, Memory and Intent candidates are also returned. | `src/character_memory/domain/models.py` |
| Prompt | `compile_context` deliberately exposes `GENERATE_IMAGE` only for allowed `USER_MESSAGE` turns. It states that general external information tools are not currently available. | `src/character_memory/runtime/context.py` |
| Autonomous opportunities | Wake/Intent, Group Autonomy, Space Opportunities and World Personal Browse have existing, different channel rules and schedules. Group Autonomy explicitly excludes background image generation. | `application/chat_service.py`, `application/group_conversation_service.py`, `space_autonomy.py`, `world_activity.py` |
| Existing implementations | Direct/Group image generation, Space media intents, World search/observation, avatar generation and avatar selection are separate public/business workflows, not a common Tool Calling protocol. | `visual_runtime.py`, `space_media_executor.py`, `world_activity.py`, `visual_web.py`, `avatar_web.py` |
| Avatar mutation | `AvatarStore` replaces the current avatar file/metadata and deletes older `avatar.*` assets; it is not a versioned rollback store. UI currently requires a user choice to set generated avatar candidates. | `avatars.py`, `web/avatars.js` |

**Do not equate a Prompt description with permission.** Model output is untrusted and must be validated again on the server. No current test should be removed merely because the future contract changes.

## 3. Parameter ownership: the critical decision

The model should express **character intent**, not infrastructure configuration:

| Layer | Owns | Avatar example |
| --- | --- | --- |
| Person / model | Whether to call, which allowed tool, minimal semantic intent | "I want a warmer, calmer portrait" |
| Stable public tool schema | Tool name, small bounded required/optional argument set, semantics and version | `avatar_refresh(visual_intent: string)` |
| Server adapter | Resolve trusted persona/current avatar, compile prompt, choose provider & model, output size, timeout, format, defaults | `VisualPromptPlanner` and existing image provider |
| Policy / execution | Character/channel scope, paid quota, idempotency, timeouts, authorization, logging, side-effect handling | Can this character refresh now? |
| Result/event | Persist actual result and allow later cognition to observe a fact, not untrusted provider instructions | Candidate created / update committed / failure |

**Design principles for parameters:**

1. Keep each tool's **public input contract minimal and semantic**. Do not pass arbitrary provider-specific knobs (API URL, API key, quality string, model name, raw prompt, storage path, character ID) to the model.
2. Derive trusted fields on the server: `character_id` from the current runtime/session, never from model arguments; Persona, references and current state from existing services.
3. Generate native JSON Schema from one authoritative typed tool-input definition where practicable; **always validate parsed arguments again server-side**. Native schema/strict modes improve model guidance but are not authorization or complete validation.
4. Reject unknown tools, unrecognized fields, wrong types, oversized strings, invalid enum values, tool IDs from another character and unsupported contexts. Do not silently translate dangerous malformed inputs.
5. Keep defaults and provider resolution on the server, not duplicated across System Prompt / tool schema / Settings. Schema changes that affect persisted requests require explicit version/migration strategy.
6. Return a **small structured result** (`status`, `reason_code`, `asset_id` if relevant), not raw secrets, unbounded HTML or private provider responses.
7. Idempotency, retry classification and quota charging are execution concerns. A network timeout on a side-effecting request must not trigger an unconditional repeat.

Illustrative native tool description, **not executable/current API**:

```json
{
  "type": "function",
  "function": {
    "name": "avatar_refresh",
    "description": "Suggest a new avatar for the current character when they genuinely want one.",
    "parameters": {
      "type": "object",
      "properties": {
        "visual_intent": {
          "type": "string",
          "description": "A brief semantic description of the desired look; not provider instructions."
        }
      },
      "required": ["visual_intent"],
      "additionalProperties": false
    }
  }
}
```

Example mapping:

```text
existing Wake opportunity + trusted character context
  -> eligible built-in tool descriptions (or empty list)
  -> model chooses no tool OR emits native tool_calls[0]
  -> parse/validate name + tiny semantic arguments
  -> enforce character/channel/side-effect policy & durable quota
  -> reuse existing avatar planner/provider/storage via thin adapter
  -> durable tool outcome; later Person context sees verified fact if useful
```

**Important unresolved choice:** `avatar_refresh` should initially **generate an avatar candidate** or **commit a new current avatar**? A candidate preserves the current user-selection boundary; automatic commit provides the requested autonomy but requires opt-in/override rules for manually selected avatars, versioned backups and rollback. "Default enabled" for use of a tool must **not** silently imply unrestricted overwrite of user-owned assets. Decide this before implementation.

## 4. Native Tool Calling versus existing structured JSON

| Concern | Native `tools/tool_calls` | Custom `PersonReaction.tool_call` JSON |
| --- | --- | --- |
| Tool parameter wire protocol | Standard shape with tool call IDs and tool-result messages | Project-owned extra schema and parser |
| Current code impact | Provider client must parse content-null/tool_calls and handle capability quirks | Small initial Pydantic change; further custom protocol debt |
| Future sequential calls | Standard continuation protocol, still requires our loop and safety policy | Need to invent/maintain continuation semantics |
| Model/backend support | Must verify actual configured model and OpenAI-compatible gateway | Usually works when structured JSON already works |
| Character cognition | Keep `PersonReaction` for human-like reactions | Temptation to blur public actions and tools in one schema |
| Input safety | Server-side validation and authorization still mandatory | Same requirement |

**Provisional direction:** prefer native Tool Calling if an offline/mock test plus the project's real model providers prove stable; preserve JSON structured-output as an **explicit, configured compatibility adapter** for a backend that cannot return correct native tool calls. Never silently reinterpret invalid native calls as safe JSON requests.

A native response can have `message.content = null` with `message.tool_calls` populated. Do not assume `response_format=json_object` and native tools compose consistently across all supported backends. The initial experiment should isolate this provider-client change instead of migrating all `react()` paths.

A **single-tool phase does not require a multi-turn tool loop**: execute zero or one validated tool call, persist the result, and let an existing later character opportunity encounter that fact. An immediate follow-up LLM call is optional and must be justified by a product requirement, rather than introduced by default. No artificial user chat message should be generated for an internal tool result.

## 5. Default availability, channel boundaries and autonomy

- Distinguish **registered** (exists in code), **available** (provider/service configured), **permitted** (role/channel/character policy), and **invoked** (model chose it). No manual plugin installation is required for built-in safe tools.
- Build the per-call tool list from effective permission and service availability; the server still enforces the same checks after model output.
- Continue using current activity opportunities. Do not create independent polling loops per tool.
- For phase one, cap at **one invoked tool per character opportunity**, even if a provider returns multiple `tool_calls`. Evaluate explicit `parallel_tool_calls=false` where supported, but never rely solely on it.
- Preserve channel policy: an internal avatar action is not a Direct message, Group message or Space post. World facts are not blindly imported as Memory; channel-specific `intent_candidates` restrictions remain.
- Preserve isolation: a character can only target its own approved assets; do not inject personal Direct memories or secret tokens into public search/tool arguments.
- No general `http.request` or arbitrary code execution by default. If introduced later, use allowlisted named endpoints and explicit network/side-effect boundaries, not user/model-supplied URLs or credentials.
- Separate low-risk read-only abilities from charged, destructive or irreversible actions. Default availability must still honor provider availability, spending ceilings, and owner override/approval where appropriate.
- On failure: do not repeatedly wake the character, charge unchecked retries or replace the current avatar partially. Record a durable failure reason.

## 6. Candidate implementation seam (not a task commitment)

| Current owner | Candidate extension; no implementation in this PR |
| --- | --- |
| `llm/client.py` | A separate native-tool request/response API and provider-compatibility adapter; avoid disrupting existing structured `PersonReaction` retry/repair behavior. |
| `runtime/context.py` + `runtime/reaction_engine.py` | Build and expose only tools eligible for the present event; do not treat Prompt as the authorization boundary. |
| `runtime/person_runtime.py` | Reuse Person context, pass a validated one-shot invocation to an application service, maintain existing channel/Memory/Intent policy. Avoid importing provider clients directly. |
| `runtime_services.py` | Reuse registered shared image/search/avatar providers; do not create a second service-locator lifecycle or Tool scheduler. |
| `visual_web.py` / `avatars.py` | Extract reusable avatar application logic if needed, keep HTTP routes thin, add version/rollback semantics before auto-commit. |
| `world_activity.py` / `space_autonomy.py` | Preserve existing scheduling and specific media/search semantics; migrate only after independent regression proof. |

This proposal must **not** be used to justify a large combined refactor of Direct, Group, Space, and World. Existing open work around Space/World lifecycle and async runtime should remain independently mergeable.

## 7. Future acceptance matrix

Each row is an intended acceptance test, **not a passing test today**.

| ID | Scenario | Expected evidence |
| --- | --- | --- |
| T01 | Eligible Wake; character prefers silence | No tool call and no paid provider call. |
| T02 | Eligible Wake; one native avatar request | Exactly one typed request, validated and dispatched to existing services. |
| T03 | Unknown tool / bad JSON / extra fields / wrong types | Denied with reason; zero side effects. |
| T04 | Multiple native tool calls in one response | Server enforces one-call budget; extra calls not executed. |
| T05 | Tool unavailable, denied for channel, or disabled for one character | Not advertised; forged output still denied. |
| T06 | Concurrent opportunities, restart and retried timeout | Durable idempotency/quota accounting; no unchecked duplicate paid work. |
| T07 | Provider errors, avatar write errors, superseded work | Original avatar survives or rolls back; failure trace present. |
| T08 | User-selected avatar and proposed autonomous replacement | Selected overwrite/approval policy enforced; reversible where allowed. |
| T09 | Cross-character asset ID / secret parameters / malicious webpage text | Access denied; no cross-person leakage or arbitrary instructions executed. |
| T10 | Tool success/failure | Trace links character, source opportunity, tool, version, sanitized args, result, elapsed time and cost; not raw chain-of-thought. |
| T11 | Direct/Group/Space/World behavior regression | Existing `PersonReaction`, ImageGen, Voice, Memory Admission, Group restrictions and World browse tests stay valid. |
| T12 | Provider matrix | Mocked OpenAI-compatible native response plus real configured backend(s); compare invalid-argument rate, abstention behavior, latency and token spend to JSON compatibility. |

Suggested existing regression anchors: `tests/test_p0_19_direct_visual_runtime.py`, `tests/test_group_autonomous_visual_contract.py`, `tests/test_space_media_executor.py`, `tests/test_world_activity.py`, `tests/test_proactive_self_loop.py`, `tests/test_avatar_route_contract.py`, `tests/test_space_autonomy.py`. New tests should cover provider wire protocol and one-shot tool execution using fake providers, without GPU, paid network calls or actual secrets. Real Provider/Chromium visual acceptance is a separate explicit step.

## 8. Review questions before any implementation PR

1. What is the exact **first** tool: avatar candidate generation, autonomous avatar replacement, or a read-only tool? Who owns overwrite approval?
2. What is the minimal typed argument contract (e.g. one `visual_intent`), and can all other parameters safely come from existing services?
3. Which current model/provider combinations actually return reliable native tool calls with `tool_choice=auto`? What compatibility mode is explicitly configured on failure?
4. How do we make a one-tool opportunity without imposing an **extra LLM call on every idle Wake**, and where does its durable budget live?
5. Is tool success a silent internal fact or does any specific tool require an immediate, separately justified reaction?
6. What are the defaults for read-only vs paid/mutating tools? Who can disable a single character's tool access?
7. How do we avoid two sources of truth between native tool argument schema, typed validation and Settings/provider defaults?
8. What runtime-visible evidence distinguishes model abstention, denied permission, unavailable provider and execution failure?

**Exit condition for this draft:** a future implementation request explicitly resolves or bounds these questions, with focused PRs and tests. There is deliberately **no implementation, configuration change, new background task, or acceptance claim** in this document.
