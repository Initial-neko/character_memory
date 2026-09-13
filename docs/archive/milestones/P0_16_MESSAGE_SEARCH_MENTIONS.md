# P0.16 — Message Search & Group Mentions

## Goal

P0.16 improves two core chat behaviors without changing the P0.15 asynchronous runtime model:

1. find a real message that happened in the past;
2. explicitly direct attention to one or more group members while keeping the room alive.

## Message search contract

### Source of truth

Message search reads durable chat Event Logs only:

- direct chat: `events` / `USER_MESSAGE`, `CHARACTER_MESSAGE`;
- group chat: `conversation_events`.

It intentionally does **not** search Memory, Mental State, runtime Trace, Intent, diary, or other derived cognition.

### Search modes

- **Current chat**: current direct character or current group only.
- **Global**: merge direct and group hits and order them by event time.

P0.16 uses parameterized SQLite `LIKE` matching. `%`, `_`, and the escape character are escaped so user input is treated literally. Semantic/vector search is intentionally out of scope for the first version.

### Deep-history navigation

A search result must be navigable, not merely displayed. Each hit gets a `jump_before_id` calculated from the durable Event ordering. The Web UI reuses the existing `before_id` history APIs and loads a window in which the hit appears with surrounding chat context.

This preserves one history pagination model instead of introducing a separate search-history renderer.

## Group mention contract

### Identity

Visible text may contain `@Rei`, `@character-id`, or `@所有人`, but durable mention identity is stored as stable Character IDs in Group Event metadata:

```json
{"mentions":["rei","momo"]}
```

`["*"]` represents `@所有人`.

### Attention, not permission

A mention changes attention and speaker priority. It does **not** create exclusive routing.

For `@Rei`:

1. Rei moves to the front of that reaction cycle's member order;
2. Rei receives an explicit strong attention/reply signal in group context;
3. every other group member still independently evaluates MESSAGE/STICKER/IMAGE/SILENCE;
4. non-mentioned members are told not to mechanically interrupt, but natural disagreement, reaction, or follow-up remains valid;
5. `actions=[]` remains legal even for the mentioned member.

For multiple mentions, mentioned members lead in first-seen order and remaining members follow the normal round-robin order.

For `@所有人`, every member is explicitly mentioned and the existing round-robin order remains intact.

### P0.15 burst/supersede behavior

Mentions participate in the same asynchronous Reaction Window as images and user facts.

If a quick burst is:

```text
@Rei 你怎么看？
@Momo 你之前不是反对吗？
```

its effective mention order is:

```text
["rei", "momo"]
```

If a generation is superseded by a later user Event, pending mention signals are not consumed. The retry uses the newest durable facts plus all unprocessed mention signals.

### Progressive delivery

Mention support does not alter P0.15 progressive group rendering. A committed member response is pushed through SSE immediately. Later members continue judging against the updated shared Event Log, and the user can insert another message at any time.

## Web interaction

- Typing `@` in a group composer opens member autocomplete.
- `@所有人` is available in the same picker.
- Keyboard: Up/Down selects, Enter/Tab inserts, Escape closes.
- Clicking a visible group speaker name inserts a mention.
- Search is available from the chat header in current-chat and global modes.
- Clicking a result loads its history window, scrolls to the Event, and highlights it.
- A “返回最新消息” control returns the view to the current tail.

## Scaling boundary

P0.16 intentionally starts with SQLite `LIKE` because it is deterministic and low-complexity. If measured history size makes full scans material, the repository layer can move to SQLite FTS5 without changing the Web/API result contract.
