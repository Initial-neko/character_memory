# Repository Guidelines

These rules apply to contributors and coding agents working in this repository.

## Project scope

Character Memory is an experimental persistent-AI-person project. Keep new capabilities attached to the same Person runtime and durable event model rather than creating parallel personality/state systems.

The current implementation is defined by:

1. source code on the target branch;
2. `docs/current/` maintained contracts;
3. `config.example.yaml` and `pyproject.toml`;
4. tests.

Historical notes under `docs/archive/` are not current API or runtime contracts.

Implementation state is owned by `docs/current/STATUS.md`. Do not infer that an open PR, old plan, archived milestone, or TODO is already shipped.

## Architecture map

The codebase is intentionally layered without a framework-heavy service container:

```text
server.py
  -> create_api()
     -> AppBundle / RuntimeServices / typed route access
  -> attach feature HTTP adapters

application/
  -> Direct/Group orchestration, async scheduling, autonomy, wake/proactive

runtime/
  -> PersonRuntime, shared PersonContextBuilder, ReactionEngine, resource gating

domain/
  -> structured contracts (Event / Memory / PersonReaction / ActionDecision)

storage/ + memory/
  -> durable facts, migrations, search/history, embedding/recall

feature modules
  -> Space, Encounter, Ensemble, Avatar, Visual, Voice, World

web/
  -> native HTML/CSS/JS product surfaces; no React build chain
```

Key ownership rules:

- `api.py` is composition/lifecycle, not a bucket for new endpoints.
- `server.py` is the fastest map of Character Runtime route assembly.
- `RuntimeServices` owns shared Search / Avatar / ImageGen / World infrastructure; route attach order must not create dependencies.
- Direct and Group share the reaction/materialization core but retain explicit channel policy.
- Space/World use the same Person data but still have channel-specific planning; do not force a universal action schema without a product reason.
- Formal Character creation converges through `ApiCharacterService` + `CharacterOnboardingService`; Ensemble and accepted Encounter candidates must not invent parallel permanent persona/onboarding paths.
- onboarding network/provider work must stay outside the character-write lock, and mandatory initialization failure must preserve rollback semantics.
- durable facts remain authoritative; Memory/Mental State/Intent/Trace are derived cognition.
- Media, Vision, ImageGen, Voice and Avatar are channels/tools of the same Person, never separate personalities.

For file-level navigation, read `docs/current/CODEBASE_LAYOUT.md` before broad edits. For runtime topology, read `docs/current/ARCHITECTURE.md`.

## Development workflow

- Make focused changes with focused tests.
- Do not commit generated task ledgers, agent transcripts, step-by-step implementation plans, or review scratchpads as product documentation.
- Durable architecture/product behavior belongs in `docs/current/`.
- Temporary implementation planning belongs in the issue/PR or local untracked notes.
- Avoid compatibility layers unless a change explicitly requires one. Prefer a clear failure over silent schema/config drift.
- Keep cross-process and file-format contracts covered by real round-trip tests.
- Do not modify a developer's real `.env` from tests. Use temporary paths.
- Never commit secrets, local model weights, generated voice clips, or machine-specific absolute paths.

## Testing

Use the repository environment:

```bash
uv run pytest -q
```

For focused changes, run the smallest relevant test set first, then the full suite before merge.

Browser smoke tests and live-model checks have separate environment requirements. CI contract tests must not require GPU access or model downloads.

## Documentation

When behavior changes:

- update the relevant contract file in `docs/current/`;
- update `docs/current/STATUS.md` when something moves between IN PROGRESS / SHIPPED / BACKLOG / DEFERRED;
- keep `README.md` limited to project overview, setup, primary architecture, stable entry points, and a short status pointer;
- keep architecture ownership reflected here and in `CODEBASE_LAYOUT.md` when modules/services move;
- use `docs/archive/` only for historical milestones that are still worth keeping;
- do not create tool-specific documentation trees such as `docs/superpowers/`.

## Releases

- Keep `main` CI-green; do normal work on short-lived branches.
- Stable baselines are immutable Git tags / GitHub Releases, not a moving `stable` branch.
- Update `pyproject.toml`, `CHANGELOG.md`, and `docs/current/RELEASES.md` when cutting a release line.
- Do not promote a release candidate to stable until the normal stack has had real local soak time in addition to CI.
- Create a `release/X.Y` maintenance branch only when a shipped stable line needs a hotfix after `main` has moved on.

## Pull requests

A PR should state:

- what changed;
- why;
- validation performed;
- known limitations or deferred work.

Do not merge an incomplete experimental feature merely because its supporting contract landed. Keep the distinction between foundation, runtime integration, and user-visible completion explicit.

Before opening a PR that changes architecture or a user-visible capability, verify that its status wording is consistent across the owning topic doc, `STATUS.md`, README summary (if affected), and this repository guide (if ownership moved). Open PR behavior must never be described as current-main behavior.
