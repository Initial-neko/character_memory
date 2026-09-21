from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from character_memory.time_utils import epoch_us, parse_datetime


MAX_COMMENTERS_PER_POST = 10


class SpacePost(BaseModel):
    id: int
    character_id: str
    content: str
    created_at: datetime
    media_id: str | None = None
    source_event_id: int | None = None
    visibility: str = "ACTIVE_CHARACTERS"


class SpaceComment(BaseModel):
    id: int
    post_id: int
    character_id: str
    content: str
    created_at: datetime
    reply_to_comment_id: int | None = None


class SpaceReaction(BaseModel):
    post_id: int
    character_id: str
    reaction_type: str
    created_at: datetime


class SpaceView(BaseModel):
    post_id: int
    character_id: str
    viewed_at: datetime


class SpaceRepository:
    """Durable social facts for Character Space.

    Space is shared world state, so posts/comments/reactions/views live once in
    shared tables instead of being copied into each character's local event log.
    Character runtimes may later observe these facts and derive their own memory
    or mental-state changes independently.
    """

    def __init__(self, store):
        self.store = store
        self._init_schema()

    def _init_schema(self) -> None:
        with self.store._lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS space_posts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    media_id TEXT,
                    source_event_id INTEGER,
                    visibility TEXT NOT NULL DEFAULT 'ACTIVE_CHARACTERS'
                );

                CREATE TABLE IF NOT EXISTS space_comments(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id INTEGER NOT NULL,
                    character_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    reply_to_comment_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS space_reactions(
                    post_id INTEGER NOT NULL,
                    character_id TEXT NOT NULL,
                    reaction_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    PRIMARY KEY(post_id, character_id, reaction_type)
                );

                CREATE TABLE IF NOT EXISTS space_views(
                    post_id INTEGER NOT NULL,
                    character_id TEXT NOT NULL,
                    viewed_at TEXT NOT NULL,
                    viewed_at_epoch INTEGER NOT NULL,
                    PRIMARY KEY(post_id, character_id)
                );

                CREATE INDEX IF NOT EXISTS idx_space_posts_created
                    ON space_posts(created_at_epoch DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_space_posts_character_created
                    ON space_posts(character_id,created_at_epoch DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_space_comments_post_created
                    ON space_comments(post_id,created_at_epoch,id);
                CREATE INDEX IF NOT EXISTS idx_space_reactions_post
                    ON space_reactions(post_id,reaction_type,created_at_epoch);
                CREATE INDEX IF NOT EXISTS idx_space_views_post
                    ON space_views(post_id,viewed_at_epoch);
                """
            )
            self.store._ensure_migration_table_locked()
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("space/001-core", datetime.now().astimezone().isoformat()),
            )
            self.store._maybe_commit()

    @staticmethod
    def _post_from_row(row) -> SpacePost:
        return SpacePost(
            id=int(row["id"]),
            character_id=str(row["character_id"]),
            content=str(row["content"]),
            created_at=parse_datetime(row["created_at"]),
            media_id=row["media_id"],
            source_event_id=row["source_event_id"],
            visibility=str(row["visibility"]),
        )

    @staticmethod
    def _comment_from_row(row) -> SpaceComment:
        return SpaceComment(
            id=int(row["id"]),
            post_id=int(row["post_id"]),
            character_id=str(row["character_id"]),
            content=str(row["content"]),
            created_at=parse_datetime(row["created_at"]),
            reply_to_comment_id=row["reply_to_comment_id"],
        )

    @staticmethod
    def _reaction_from_row(row) -> SpaceReaction:
        return SpaceReaction(
            post_id=int(row["post_id"]),
            character_id=str(row["character_id"]),
            reaction_type=str(row["reaction_type"]),
            created_at=parse_datetime(row["created_at"]),
        )

    def create_post(
        self,
        character_id: str,
        content: str,
        now: datetime,
        *,
        media_id: str | None = None,
        source_event_id: int | None = None,
        visibility: str = "ACTIVE_CHARACTERS",
    ) -> SpacePost:
        clean_content = str(content or "").strip()
        clean_media = str(media_id or "").strip() or None
        if not clean_content and clean_media is None:
            raise ValueError("space post requires content or media")
        stamp = epoch_us(now)
        with self.store._lock:
            cur = self.store.conn.execute(
                "INSERT INTO space_posts(character_id,content,created_at,created_at_epoch,media_id,source_event_id,visibility) "
                "VALUES(?,?,?,?,?,?,?)",
                (character_id, clean_content, now.isoformat(), stamp, clean_media, source_event_id, visibility),
            )
            self.store._maybe_commit()
        return SpacePost(
            id=int(cur.lastrowid),
            character_id=character_id,
            content=clean_content,
            created_at=now,
            media_id=clean_media,
            source_event_id=source_event_id,
            visibility=visibility,
        )

    def get_post(self, post_id: int) -> SpacePost | None:
        with self.store._lock:
            row = self.store.conn.execute("SELECT * FROM space_posts WHERE id=?", (int(post_id),)).fetchone()
        return self._post_from_row(row) if row is not None else None

    def list_posts(
        self,
        *,
        character_id: str | None = None,
        limit: int = 10,
        before_id: int | None = None,
    ) -> tuple[list[SpacePost], bool, int | None]:
        page_size = max(1, min(int(limit), 50))
        sql = "SELECT * FROM space_posts WHERE visibility='ACTIVE_CHARACTERS'"
        args: list[Any] = []
        if character_id:
            sql += " AND character_id=?"
            args.append(character_id)
        if before_id is not None:
            cursor = self.get_post(int(before_id))
            if cursor is None:
                return [], False, None
            cursor_epoch = epoch_us(cursor.created_at)
            sql += " AND (created_at_epoch<? OR (created_at_epoch=? AND id<?))"
            args.extend([cursor_epoch, cursor_epoch, int(before_id)])
        sql += " ORDER BY created_at_epoch DESC,id DESC LIMIT ?"
        args.append(page_size + 1)
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        posts = [self._post_from_row(row) for row in rows]
        next_before_id = posts[-1].id if has_more and posts else None
        return posts, has_more, next_before_id

    def count_posts(self, *, character_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS total FROM space_posts WHERE visibility='ACTIVE_CHARACTERS'"
        args: list[Any] = []
        if character_id:
            sql += " AND character_id=?"
            args.append(character_id)
        with self.store._lock:
            row = self.store.conn.execute(sql, args).fetchone()
        return int(row["total"] if row is not None else 0)

    def list_comments(self, post_id: int) -> list[SpaceComment]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM space_comments WHERE post_id=? ORDER BY created_at_epoch,character_id",
                (int(post_id),),
            ).fetchall()
        return [self._comment_from_row(row) for row in rows]

    def add_comment(
        self,
        post_id: int,
        character_id: str,
        content: str,
        now: datetime,
        *,
        reply_to_comment_id: int | None = None,
    ) -> SpaceComment:
        if self.get_post(post_id) is None:
            raise KeyError("space post not found")
        clean_content = str(content or "").strip()
        if not clean_content:
            raise ValueError("space comment must not be empty")
        if reply_to_comment_id is not None:
            with self.store._lock:
                parent = self.store.conn.execute(
                    "SELECT post_id FROM space_comments WHERE id=?",
                    (int(reply_to_comment_id),),
                ).fetchone()
            if parent is None or int(parent["post_id"]) != int(post_id):
                raise ValueError("reply target does not belong to this post")

        with self.store._lock:
            existing = self.store.conn.execute(
                "SELECT 1 FROM space_comments WHERE post_id=? AND character_id=? LIMIT 1",
                (int(post_id), character_id),
            ).fetchone()
            if existing is None:
                row = self.store.conn.execute(
                    "SELECT COUNT(DISTINCT character_id) AS total FROM space_comments WHERE post_id=?",
                    (int(post_id),),
                ).fetchone()
                if int(row["total"] if row is not None else 0) >= MAX_COMMENTERS_PER_POST:
                    raise ValueError(f"a space post may have at most {MAX_COMMENTERS_PER_POST} character commenters")
            cur = self.store.conn.execute(
                "INSERT INTO space_comments(post_id,character_id,content,created_at,created_at_epoch,reply_to_comment_id) "
                "VALUES(?,?,?,?,?,?)",
                (int(post_id), character_id, clean_content, now.isoformat(), epoch_us(now), reply_to_comment_id),
            )
            self.store._maybe_commit()
        return SpaceComment(
            id=int(cur.lastrowid),
            post_id=int(post_id),
            character_id=character_id,
            content=clean_content,
            created_at=now,
            reply_to_comment_id=reply_to_comment_id,
        )

    def list_reactions(self, post_id: int, reaction_type: str = "LIKE") -> list[SpaceReaction]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM space_reactions WHERE post_id=? AND reaction_type=? "
                "ORDER BY created_at_epoch,character_id",
                (int(post_id), reaction_type),
            ).fetchall()
        return [self._reaction_from_row(row) for row in rows]

    def set_reaction(
        self,
        post_id: int,
        character_id: str,
        reaction_type: str,
        enabled: bool,
        now: datetime,
    ) -> bool:
        if self.get_post(post_id) is None:
            raise KeyError("space post not found")
        normalized = str(reaction_type or "").strip().upper()
        if normalized != "LIKE":
            raise ValueError("unsupported space reaction")
        with self.store._lock:
            if enabled:
                self.store.conn.execute(
                    "INSERT INTO space_reactions(post_id,character_id,reaction_type,created_at,created_at_epoch) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(post_id,character_id,reaction_type) DO UPDATE SET "
                    "created_at=excluded.created_at,created_at_epoch=excluded.created_at_epoch",
                    (int(post_id), character_id, normalized, now.isoformat(), epoch_us(now)),
                )
            else:
                self.store.conn.execute(
                    "DELETE FROM space_reactions WHERE post_id=? AND character_id=? AND reaction_type=?",
                    (int(post_id), character_id, normalized),
                )
            self.store._maybe_commit()
        return enabled

    def record_view(self, post_id: int, character_id: str, now: datetime) -> SpaceView:
        if self.get_post(post_id) is None:
            raise KeyError("space post not found")
        with self.store._lock:
            self.store.conn.execute(
                "INSERT INTO space_views(post_id,character_id,viewed_at,viewed_at_epoch) VALUES(?,?,?,?) "
                "ON CONFLICT(post_id,character_id) DO UPDATE SET viewed_at=excluded.viewed_at,viewed_at_epoch=excluded.viewed_at_epoch",
                (int(post_id), character_id, now.isoformat(), epoch_us(now)),
            )
            self.store._maybe_commit()
        return SpaceView(post_id=int(post_id), character_id=character_id, viewed_at=now)

    def list_views(self, post_id: int) -> list[SpaceView]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM space_views WHERE post_id=? ORDER BY viewed_at_epoch",
                (int(post_id),),
            ).fetchall()
        return [
            SpaceView(
                post_id=int(row["post_id"]),
                character_id=str(row["character_id"]),
                viewed_at=parse_datetime(row["viewed_at"]),
            )
            for row in rows
        ]
