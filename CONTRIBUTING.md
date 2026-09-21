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

Run focused tests while developing, then run:

```bash
uv run pytest -q
```

If your change affects browser behavior, also run the relevant browser smoke checks. If it affects a real local model or CUDA sidecar, document the live validation separately from CI.

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
