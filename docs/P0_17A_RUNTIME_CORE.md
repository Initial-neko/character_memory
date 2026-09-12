# P0.17A — Runtime Core

This delivery intentionally contains only two runtime hardening changes:

1. schema-aware structured-output repair prompts;
2. Mental State history deduplication.

It does not contain SSE reconciliation, Character Wake, Trace UI restructuring, Playwright infrastructure, dependency locking, Memory architecture changes, DB schema changes, or crash recovery.

## Structured-output repair

When the provider returns invalid JSON for a Pydantic target, the retry prompt must describe the actual target schema. `PersonReaction` keeps its action/resource-specific repair contract; other schemas receive their own schema name, field names, and validation error summary instead of being told to return `actions`.

## Mental State history

`set_mental_state()` normalizes whitespace and creates a new history row only when the effective state differs from the latest state at that timestamp. Existing history is not migrated or rewritten.
