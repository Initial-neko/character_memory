#!/usr/bin/env python
"""Reject new Direct-conversation writes that bypass ``ConversationFactWriter``.

The W3 contract gives chat facts exactly one writer. A stray
``INSERT INTO events ... 'USER_MESSAGE'`` or a direct
``INSERT INTO conversation_events`` in product code would recreate the second
source of truth the migration removed, and no runtime test would notice until a
message went missing from history.

This guard is deliberately static and small: it flags new raw INSERT statements
outside an explicit allow-list of layers that are *supposed* to own SQL
(storage primitives, the migration script, test fixtures). File-level allow-listing
is what the ADR requires; classifying by event type would let a new bypass hide
behind a helper.

Run in CI: ``python scripts/check_conversation_fact_writes.py``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "character_memory"

# Files that legitimately contain raw conversation-fact SQL.
ALLOWED = {
    # The one writer/reader pair and the schema it owns.
    "storage/conversation_facts.py",
    # Legacy event-log primitive; the guard lives here and refuses chat types.
    "storage/sqlite.py",
    # Group domain storage: the shared-fact primitive and its own queries.
    "group_store.py",
}

INSERT_EVENTS = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+events\b", re.IGNORECASE)
INSERT_CONVERSATION = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+conversation_events\b", re.IGNORECASE
)

SKIP_SUFFIXES = {".pyc"}


def rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def scan() -> list[str]:
    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.suffix in SKIP_SUFFIXES:
            continue
        name = rel(path)
        if name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if INSERT_EVENTS.search(line):
                problems.append(f"{name}:{line_number}: writes to legacy events; use ConversationFactWriter.append_direct")
            if INSERT_CONVERSATION.search(line):
                problems.append(
                    f"{name}:{line_number}: writes conversation_events directly; "
                    "use ConversationFactWriter or GroupRepository"
                )
    return problems


def main() -> int:
    problems = scan()
    if problems:
        print("conversation-fact write guard failed:", file=sys.stderr)
        for item in problems:
            print(f"  {item}", file=sys.stderr)
        print(
            "\nDirect chat facts have one writer (ConversationFactWriter). "
            "If this file really must own raw SQL, add it to ALLOWED with a reason.",
            file=sys.stderr,
        )
        return 1
    print(f"conversation-fact write guard passed ({len(list(SRC.rglob('*.py')))} modules scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
