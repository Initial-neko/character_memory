# Memory Architecture — Current State and Next Design Questions

> P0.17 is documentation only. This file deliberately does not lock a new database schema or migration.

## 1. Product principle

Memory belongs to the **Character**, not to a conversation silo.

A character who remembers something from a private chat remains the same character when entering a group. Whether that memory is appropriate to mention is a behavioral/social judgment, not a storage-level amnesia rule. This is an intentional SOUL property of Character Memory.

The Event Log remains the source of truth. Memory is derived cognition: a compact, selective interpretation of past experience that can later be recalled.

## 2. Current Memory object

Current persisted Memory contains:

- `id`
- `character_id`
- `content`
- `memory_type`
- `event_time`
- `importance`
- `source_event_id`
- `active`
- `metadata`
- `embedding`

`active` currently acts mainly as a recall filter. There is not yet a complete lifecycle such as reinforced / superseded / retracted.

## 3. Current write path

For ordinary direct/group reactions the model returns `memory_candidates`.

Admission is intentionally small:

1. empty or `importance < 0.35` -> `SKIP_LOW_VALUE`;
2. exact normalized duplicate -> `SKIP_DUPLICATE`;
3. embedding cosine similarity `>= 0.93` to an existing active Memory -> `SKIP_DUPLICATE`;
4. otherwise -> `WRITE`.

This prevents obvious low-value repetition but does **not** understand contradiction, correction, changed preferences, or repeated reinforcement.

Example that current admission cannot model correctly:

```text
old: User likes coffee.
new: User no longer likes coffee.
```

The two statements may be embedding-near even though the meaning changed. The new fact can be incorrectly skipped as a duplicate, or both can remain active and later be recalled together.

## 4. Current recall path

Recall is character-global over active Memory.

Current ranking roughly combines:

- semantic similarity: 70%
- recency: 20%
- importance: 10%

and returns top-k (`recall_limit`, currently commonly 8).

There is not yet a relevance threshold: when all memories are weakly related, the least-bad top-k can still enter context.

Group-origin memories carry provenance metadata such as `origin=GROUP` and conversation/source identifiers, but they remain part of the same character cognition rather than a separate memory silo.

## 5. Other current producers

Direct/group PersonRuntime uses the Memory Admission path described above.

Older/frozen Life Simulation and Diary flows have historically written memories through different paths. Because Life Simulation is frozen in the current product phase, P0.17 does not attempt to unify those writers.

## 6. Problems the next Memory design must answer

The next design discussion should start from cognition/product semantics, not from adding columns.

### What is one Memory?

Is a Memory primarily:

- a factual belief (`User likes coffee`),
- an episodic experience (`We talked until 2am about the project`),
- a relationship interpretation (`They trusted me with something private`),
- a preference/habit,
- or a compact mixture?

Different categories may need different update behavior.

### What should repetition do?

Repeated experiences should not necessarily produce ten near-identical rows. Possible behavior:

- ignore as duplicate;
- **REINFORCE** an existing memory;
- create a distinct episode while reinforcing a higher-level belief.

We need to decide which semantics belong to the model versus deterministic admission.

### What happens when reality changes?

Possible lifecycle vocabulary for discussion:

- **ADD** — a genuinely new memory;
- **REINFORCE** — repeated evidence strengthens an existing memory;
- **SUPERSEDE** — a newer interpretation/fact replaces an older active belief without deleting history;
- **RETRACT** — a memory is recognized as wrong/unreliable and should no longer influence normal recall.

These are candidate concepts, not yet an approved schema.

### How should contradiction work?

Embedding similarity is not contradiction detection. The system needs a way to distinguish:

```text
likes coffee
likes coffee very much
no longer likes coffee
was pretending to like coffee
```

This may require an LLM-assisted memory decision over a small set of candidate memories rather than a cosine threshold alone.

### What does forgetting mean?

`active=false` currently only means “do not recall normally.” Future design needs to decide whether forgetting is:

- decay,
- explicit retraction,
- supersession,
- low confidence,
- or simply retrieval ranking.

Event history should remain immutable even when derived Memory changes.

## 7. Constraints for the next Memory iteration

Any future design should preserve these invariants:

1. Event Log is immutable source-of-truth history.
2. Memory is derived and may be revised without rewriting the Event Log.
3. Memory remains character-global so the same person carries experience across direct/group contexts.
4. Superseded/retracted cognition must remain inspectable for debugging/provenance rather than being hard-deleted.
5. Recall should normally exclude cognition that is no longer considered active/current.
6. Memory decisions should be traceable: why a candidate was added, reinforced, superseded, retracted, or ignored.
7. Avoid turning every chat line into a permanent fact.

## 8. Next review agenda

Before implementation, explicitly decide:

1. the semantic categories of Memory;
2. whether one row represents an episode, a belief, or both;
3. rules for reinforcement;
4. rules for changed facts/preferences;
5. contradiction handling;
6. confidence/importance versus recurrence;
7. whether recall needs a relevance threshold;
8. how provenance should be presented in Runtime Trace.

Only after those decisions should a DB/schema proposal be written.
