"""Per-character activity counters for the archive drawer.

Archiving hides a character from the sidebar without touching anything it
produced, so before restoring or deleting one the user needs to see whether it
was ever actually active. Three numbers answer that: posts it published in
Space, comments it wrote there, and messages it spoke.

All three count **the character's own output**, never the other side of the
conversation. That keeps them comparable -- "this one wrote 137 things" -- and
stops the user's own messages from making a silent character look busy. The
counters live next to the tables they read: ``SpaceRepository.count_posts`` /
``count_comments``, ``Storage.count_character_messages`` for private chats and
``GroupRepository.count_character_messages`` for group turns. This module only
composes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from character_memory.group_store import GroupRepository
from character_memory.space_store import SpaceRepository


@dataclass(frozen=True)
class CharacterActivity:
    posts: int
    comments: int
    messages: int

    def payload(self) -> dict[str, int]:
        return {
            "posts": self.posts,
            "comments": self.comments,
            "messages": self.messages,
        }


class CharacterActivityReader:
    """Read-only activity counters, constructed once per route table.

    Holds its repositories instead of rebuilding them per request: both
    constructors run ``CREATE TABLE IF NOT EXISTS``, which is idempotent but
    has no business running on a read path.
    """

    def __init__(self, read_store: Any):
        self._store = read_store
        self._space = SpaceRepository(read_store)
        self._groups = GroupRepository(read_store)

    def for_character(self, character_id: str) -> CharacterActivity:
        return CharacterActivity(
            posts=self._space.count_posts(character_id=character_id),
            comments=self._space.count_comments(character_id=character_id),
            messages=(
                self._store.count_character_messages(character_id)
                + self._groups.count_character_messages(character_id)
            ),
        )
