# Contributing

Character Memory is an experimental project under active development. Contributions should keep the repository easy to run, test, and understand from the current tree rather than from historical implementation notes.

## Development setup

Requirements:

- Python 3.12
- `uv`
- Git Bash on Windows is a supported development path

Prepare the full local environment:

```bash
git clone https://github.com/Initial-neko/character_memory.git
cd character_memory
bash scripts/setup-media-models.sh
uv run character-memory init
```

Start the normal development stack:

```bash
uv run character-stack
```

Primary local services:

| Service | Address |
| --- | --- |
| Character Runtime | `http://127.0.0.1:8000` |
| Media Runtime | `http://127.0.0.1:8001` |
| Dev Console | `http://127.0.0.1:8002/dev` |
| Settings Center | `http://127.0.0.1:8003/settings` |
| TTS Workbench / Provider Runtime | `http://127.0.0.1:9002/tts` |

Optional sidecars are documented in `docs/current/`.

## Before opening a pull request

Acceptance fixtures must use temporary configuration/database paths and isolated service origins. `config.example.yaml` uses a separate example database; it is not permission to point a second `create_dev_app`/Core at the formal database. Formal inspection uses read-only SQLite queries; backup uses the SQLite online backup API, never overwriting the formal database with an acceptance fixture.

Run focused tests while developing, then run:

```bash
uv run pytest -q
```

If your change affects browser behavior, also run the relevant browser smoke checks. If it affects a real local model or CUDA sidecar, document the live validation separately from CI.

## Documentation checks before submission

Run `python scripts/check_docs.py` before each commit. It checks local link targets in maintained documents and requires every `docs/current/*.md` to be indexed in `docs/README.md`. It does not fetch external links or validate Markdown heading anchors.

Every PR must include three plain-text fields (not only checked boxes):

```text
Docs-Impact: updated
Docs-Reason: Describe which durable contract changed and how the document now matches it.
Docs-Contracts: docs/current/CONVERSATION_RUNTIME.md
```

Use `Docs-Impact: none` with a concrete `Docs-Reason` when behavior, configuration, persistence, lifecycle and public entry points are unchanged. Tests and internal refactors do not automatically require prose edits. With `updated`, list the actual changed maintained documents in `Docs-Contracts`, separated by commas.

Run the same PR impact check locally using a UTF-8 PR body file:

```bash
python scripts/check_docs.py --base origin/main --pr-body /path/to/pr-body.md
```

The `Documentation / docs-contract` CI job runs on every PR push and body edit, and checks structure on main pushes. Missing impact declarations, placeholder reasons, broken local links, unindexed current contracts, or claimed document updates absent from the diff fail the check. No model dependencies are installed for this job. To make it a merge requirement, repository rules must require this status check; adding the workflow alone does not change branch protection.

Automation checks evidence and completeness, not behavioral truth. The contributor still reads the owning contract via `docs/README.md`, checks it against current source and behavioral tests, replaces stale statements, and updates `CODEBASE_LAYOUT` when a modification entry moves or is introduced. Reviewers must check that the listed contract owns the change and that a `none` reason is valid. Keep capability lists/defaults in their owning contract or source schema and link there from architecture summaries rather than copying them into multiple maintained documents. Do not auto-generate prose, append scan logs, or change unrelated documents to satisfy CI.

## Branches and releases

Use short-lived feature/fix/refactor/chore branches and merge them into a CI-green `main`. Do not maintain a permanent `develop` or moving `stable` branch.

Work is finished when it is a pushed branch with an open PR, not when it runs on your machine. Do not leave uncommitted work sitting on `main`. A change that exists only in a working tree cannot be reviewed, cannot be seen by CI, and cannot be merged, so it is invisible to every other contributor and quietly drifts from `main` as the branch moves on. Finish by committing to a branch and opening the PR, even when the change feels too small to be worth one.

Stage the paths your change owns — `git add <path>`, or `git add -u` for already-tracked files — rather than `git add -A`. Generated media, local databases, model assets and runtime character data live in the working tree untracked, so a blanket add sweeps them into the commit.

Release baselines use immutable tags and GitHub Releases. See `docs/current/RELEASES.md` for the 0.x version rules, RC promotion flow, and when a temporary `release/X.Y` maintenance branch is appropriate.

## Documentation rules

The maintained documentation layout is:

```text
README.md
CONTRIBUTING.md
AGENTS.md
docs/
├─ README.md
├─ current/
├─ research/
└─ archive/
```

Use `docs/current/` for behavior that is true on the target branch. Historical milestone notes may live under `docs/archive/`, but implementation checklists, agent logs, and temporary planning documents should stay in the PR/issue or outside the tracked repository.

## Configuration and secrets

- `config.yaml` contains non-secret runtime choices.
- `.env` contains local secrets and selected local runtime asset paths.
- Do not commit real API keys, tokens, model weights, generated reference audio, or private local paths.
- Tests that exercise settings persistence must use temporary files rather than a developer's real `.env`.

## Change design

Prefer the smallest architecture that preserves current invariants:

- Events are the durable facts.
- Voice, vision, image generation, and capture are channels of the same Person.
- Slow optional providers must not break the text conversation path.
- Cross-service schemas and on-disk formats require round-trip tests.
- Avoid introducing queues, databases, frameworks, or compatibility layers without a demonstrated need.

See `docs/README.md` for the documentation index and `docs/current/CODEBASE_LAYOUT.md` for source navigation.
