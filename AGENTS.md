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

## Development workflow

- Make focused changes with focused tests.
- Do not commit generated task ledgers, agent transcripts, step-by-step implementation plans, or review scratchpads as product documentation.
- Durable architecture/product behavior belongs in `docs/current/`.
- Prefer extending an existing stable-domain document. Add a new `docs/current/*.md` only for an independent runtime boundary, lifecycle, or durable ownership reason; do not create one document per provider, UI widget, PR, or feature slice.
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

- update the relevant file in `docs/current/`;
- keep `README.md` limited to project overview, setup, primary architecture, and stable entry points;
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