# Repository Guidelines

This file contains repository-wide rules and context routing for contributors and coding agents.

Keep it short and stable. Detailed architecture, feature behavior, task state, and historical notes belong elsewhere.

## Core invariants

- `main` is the integration source of truth.
- Extend the existing Person runtime, durable event model, Memory model, and shared services instead of creating parallel systems.
- Prefer focused branches, focused PRs, and focused tests.
- Do not commit agent transcripts, generated task ledgers, review scratchpads, temporary implementation plans, secrets, model assets, or machine-specific paths.
- Maintained documentation describes the current project. Git, Issues, PRs, tags, and releases preserve history.

## Source precedence

When sources disagree, use this order:

1. current source code and schemas;
2. tests that exercise current behavior;
3. maintained contracts under `docs/current/`;
4. configuration files such as `config.example.yaml` and `pyproject.toml`;
5. README / release documentation;
6. archive, research, and historical notes.

An open PR is work in progress, not current-main behavior.

## Progressive context loading

Do not preload the whole documentation tree.

At the start of a task:

1. read this file;
2. read the originating Issue / PR when applicable;
3. identify the affected subsystem;
4. read only the relevant maintained contract;
5. inspect the related source and tests.

Use these routers only when needed:

- cannot find the code owner → `docs/current/CODEBASE_LAYOUT.md`
- cannot find the owning document → `docs/README.md`
- need project-wide implementation status → `docs/current/STATUS.md`
- change crosses subsystem/runtime boundaries → `docs/current/ARCHITECTURE.md`
- development, testing, PR, or validation rules → `CONTRIBUTING.md`
- release/version/tag work → `docs/current/RELEASES.md`

Do not read all of `docs/current/` by default.
Do not read `docs/archive/`, `docs/research/`, or `CHANGELOG.md` unless the task specifically requires them.

## Documentation updates

When a PR changes durable behavior, update the owning maintained document in the same PR.

Replace stale statements with the current truth instead of appending correction history.
Avoid duplicating the same fact across multiple maintained documents.

Use `docs/README.md` for documentation ownership and routing.

## Multi-agent work

Issues and PRs are the shared coordination surface.

Before broad parallel work, check open PRs for overlapping ownership.
Parallelize independent modules; serialize strongly coupled or overlapping changes.
Do not create persistent agent-specific handoff or task-state documents.

## Release boundary

Ordinary feature work must not casually change versions, tags, or release state.
Follow `docs/current/RELEASES.md` for release work.
