from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from pydantic import BaseModel, Field

from character_memory.time_utils import epoch_us, parse_datetime


MAX_COMMENTERS_PER_POST = 10
MAX_SPACE_MENTIONS = 4


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
    actor_type: str = "CHARACTER"
    content: str
    sticker_id: str | None = None
    created_at: datetime
    reply_to_comment_id: int | None = None
    mentions: list[str] = Field(default_factory=list)
    mentions_user: bool = False
    idempotent_replay: bool = Field(default=False, exclude=True)


class SpaceNotification(BaseModel):
    id: int
    recipient_id: str = "user"
    post_id: int
    comment_id: int
    reasons: list[str]
    created_at: datetime
    read_at: datetime | None = None


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
                    actor_type TEXT NOT NULL DEFAULT 'CHARACTER',
                    content TEXT NOT NULL,
                    sticker_id TEXT,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    reply_to_comment_id INTEGER,
                    mentions_json TEXT NOT NULL DEFAULT '[]',
                    mentions_user INTEGER NOT NULL DEFAULT 0,
                    client_request_id TEXT
                );

                CREATE TABLE IF NOT EXISTS space_notifications(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient_id TEXT NOT NULL DEFAULT 'user',
                    post_id INTEGER NOT NULL,
                    comment_id INTEGER NOT NULL UNIQUE,
                    reasons_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    read_at TEXT,
                    read_at_epoch INTEGER
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

                CREATE TABLE IF NOT EXISTS space_daily_runs(
                    character_id TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    scheduled_for_epoch INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    started_at_epoch INTEGER NOT NULL,
                    completed_at TEXT,
                    completed_at_epoch INTEGER,
                    status TEXT NOT NULL,
                    post_id INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(character_id, local_date)
                );

                CREATE TABLE IF NOT EXISTS space_opportunity_state(
                    character_id TEXT PRIMARY KEY,
                    last_opportunity_at TEXT,
                    last_opportunity_at_epoch INTEGER,
                    next_opportunity_at TEXT NOT NULL,
                    next_opportunity_at_epoch INTEGER NOT NULL,
                    configured_interval_minutes REAL NOT NULL DEFAULT 0,
                    last_status TEXT,
                    last_post_id INTEGER,
                    updated_at TEXT NOT NULL,
                    updated_at_epoch INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS space_opportunity_runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    scheduled_for_epoch INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    started_at_epoch INTEGER NOT NULL,
                    completed_at TEXT,
                    completed_at_epoch INTEGER,
                    status TEXT NOT NULL,
                    post_id INTEGER,
                    source TEXT NOT NULL DEFAULT 'SCHEDULED',
                    error TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_space_daily_runs_schedule
                    ON space_daily_runs(local_date,status,scheduled_for_epoch);
                CREATE INDEX IF NOT EXISTS idx_space_opportunity_state_due
                    ON space_opportunity_state(next_opportunity_at_epoch,character_id);
                CREATE INDEX IF NOT EXISTS idx_space_opportunity_runs_character
                    ON space_opportunity_runs(character_id,started_at_epoch DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_space_posts_created
                    ON space_posts(created_at_epoch DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_space_posts_character_created
                    ON space_posts(character_id,created_at_epoch DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_space_comments_post_created
                    ON space_comments(post_id,created_at_epoch,id);
                CREATE INDEX IF NOT EXISTS idx_space_notifications_recipient_unread
                    ON space_notifications(recipient_id,read_at,created_at_epoch DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_space_reactions_post
                    ON space_reactions(post_id,reaction_type,created_at_epoch);
                CREATE INDEX IF NOT EXISTS idx_space_views_post
                    ON space_views(post_id,viewed_at_epoch);
                """
            )
            columns = {
                str(row["name"]) for row in self.store.conn.execute(
                    "PRAGMA table_info(space_opportunity_runs)"
                ).fetchall()
            }
            if "details_json" not in columns:
                self.store.conn.execute(
                    "ALTER TABLE space_opportunity_runs ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'"
                )
            state_columns = {
                str(row["name"]) for row in self.store.conn.execute(
                    "PRAGMA table_info(space_opportunity_state)"
                ).fetchall()
            }
            if "configured_interval_minutes" not in state_columns:
                self.store.conn.execute(
                    "ALTER TABLE space_opportunity_state ADD COLUMN configured_interval_minutes REAL NOT NULL DEFAULT 0"
                )
            comment_columns = {
                str(row["name"])
                for row in self.store.conn.execute("PRAGMA table_info(space_comments)").fetchall()
            }
            if "actor_type" not in comment_columns:
                self.store.conn.execute(
                    "ALTER TABLE space_comments ADD COLUMN actor_type TEXT NOT NULL DEFAULT 'CHARACTER'"
                )
            if "sticker_id" not in comment_columns:
                self.store.conn.execute(
                    "ALTER TABLE space_comments ADD COLUMN sticker_id TEXT"
                )
            if "mentions_json" not in comment_columns:
                self.store.conn.execute(
                    "ALTER TABLE space_comments ADD COLUMN mentions_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "mentions_user" not in comment_columns:
                self.store.conn.execute(
                    "ALTER TABLE space_comments ADD COLUMN mentions_user INTEGER NOT NULL DEFAULT 0"
                )
            if "client_request_id" not in comment_columns:
                self.store.conn.execute(
                    "ALTER TABLE space_comments ADD COLUMN client_request_id TEXT"
                )
            self.store.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_space_comments_client_request "
                "ON space_comments(client_request_id) WHERE client_request_id IS NOT NULL"
            )
            self.store._ensure_migration_table_locked()
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("space/001-core", datetime.now().astimezone().isoformat()),
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("space/002-daily-runs", datetime.now().astimezone().isoformat()),
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("space/003-interval-opportunities", datetime.now().astimezone().isoformat()),
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("space/005-comment-actors", datetime.now().astimezone().isoformat()),
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("space/006-comment-stickers", datetime.now().astimezone().isoformat()),
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("space/007-bidirectional-mentions-notifications", datetime.now().astimezone().isoformat()),
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
        try:
            mentions = json.loads(row["mentions_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            mentions = []
        if not isinstance(mentions, list):
            mentions = []
        return SpaceComment(
            id=int(row["id"]),
            post_id=int(row["post_id"]),
            character_id=str(row["character_id"]),
            # A comment stored as written by the browser user must not come back
            # as a character comment: the actor decides whose name is shown and
            # whether the comment spends one of the ten character slots.
            actor_type=str(row["actor_type"] or "CHARACTER"),
            content=str(row["content"]),
            sticker_id=row["sticker_id"],
            created_at=parse_datetime(row["created_at"]),
            reply_to_comment_id=row["reply_to_comment_id"],
            mentions=[str(item) for item in mentions if str(item).strip()],
            mentions_user=bool(row["mentions_user"]),
        )

    @staticmethod
    def _notification_from_row(row) -> SpaceNotification:
        try:
            reasons = json.loads(row["reasons_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []
        return SpaceNotification(
            id=int(row["id"]),
            recipient_id=str(row["recipient_id"]),
            post_id=int(row["post_id"]),
            comment_id=int(row["comment_id"]),
            reasons=[str(item) for item in reasons],
            created_at=parse_datetime(row["created_at"]),
            read_at=parse_datetime(row["read_at"]) if row["read_at"] else None,
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

    def count_posts_since(self, character_id: str, since: datetime) -> int:
        """Posts one character has published at or after ``since``.

        Backs the per-day publishing ceiling, so it counts the author's own
        posts only -- reactions and comments are not a publishing budget.
        """
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS total FROM space_posts WHERE character_id=? AND created_at_epoch>=?",
                (character_id, epoch_us(since)),
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def count_comments(self, *, character_id: str) -> int:
        """Comments one character wrote, under posts by anyone.

        The author is the commenter, not the post's owner, so this stays
        comparable with :meth:`count_posts` and reads as "things this
        character did". ``actor_type`` is filtered because a post's own
        author can also reply to itself through the same table.
        """
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS total FROM space_comments WHERE character_id=? AND actor_type='CHARACTER'",
                (character_id,),
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def list_comments(self, post_id: int) -> list[SpaceComment]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM space_comments WHERE post_id=? ORDER BY created_at_epoch,id",
                (int(post_id),),
            ).fetchall()
        return [self._comment_from_row(row) for row in rows]

    def get_comment(self, comment_id: int) -> SpaceComment | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM space_comments WHERE id=?",
                (int(comment_id),),
            ).fetchone()
        return self._comment_from_row(row) if row is not None else None

    def root_comment_id(self, comment_id: int) -> int | None:
        comment = self.get_comment(comment_id)
        if comment is None:
            return None
        seen: set[int] = set()
        while comment.reply_to_comment_id is not None:
            if comment.id in seen:
                break
            seen.add(comment.id)
            parent = self.get_comment(comment.reply_to_comment_id)
            if parent is None or parent.post_id != comment.post_id:
                break
            comment = parent
        return comment.id

    def find_comment_request_replay(
        self,
        post_id: int,
        character_id: str,
        content: str,
        *,
        actor_type: str = "CHARACTER",
        reply_to_comment_id: int | None = None,
        sticker_id: str | None = None,
        mentions: list[str] | None = None,
        mentions_user: bool = False,
        client_request_id: str | None = None,
    ) -> SpaceComment | None:
        """Return an identical durable request, or reject key reuse with new data."""

        request_id = str(client_request_id or "").strip() or None
        if not request_id:
            return None
        normalized_actor_type = str(actor_type or "CHARACTER").strip().upper()
        clean_content = str(content or "").strip()
        clean_sticker_id = str(sticker_id or "").strip() or None
        clean_mentions = [str(item or "").strip() for item in (mentions or [])]
        clean_mentions = [item for item in clean_mentions if item]
        with self.store._lock:
            existing = self.store.conn.execute(
                "SELECT * FROM space_comments WHERE client_request_id=?",
                (request_id,),
            ).fetchone()
        if existing is None:
            return None
        existing_comment = self._comment_from_row(existing)
        same_request = (
            existing_comment.post_id == int(post_id)
            and existing_comment.character_id == character_id
            and existing_comment.actor_type == normalized_actor_type
            and existing_comment.content == clean_content
            and existing_comment.sticker_id == clean_sticker_id
            and existing_comment.reply_to_comment_id == reply_to_comment_id
            and existing_comment.mentions == clean_mentions
            and existing_comment.mentions_user == bool(mentions_user)
        )
        if not same_request:
            raise ValueError("client_request_id was already used for a different Space comment")
        return existing_comment.model_copy(update={"idempotent_replay": True})

    def add_comment(
        self,
        post_id: int,
        character_id: str,
        content: str,
        now: datetime,
        *,
        actor_type: str = "CHARACTER",
        reply_to_comment_id: int | None = None,
        sticker_id: str | None = None,
        mentions: list[str] | None = None,
        mentions_user: bool = False,
        client_request_id: str | None = None,
    ) -> SpaceComment:
        if self.get_post(post_id) is None:
            raise KeyError("space post not found")
        normalized_actor_type = str(actor_type or "CHARACTER").strip().upper()
        if normalized_actor_type not in {"CHARACTER", "USER"}:
            raise ValueError("space comment actor_type must be CHARACTER or USER")
        clean_content = str(content or "").strip()
        clean_sticker_id = str(sticker_id or "").strip() or None
        clean_mentions = [str(item or "").strip() for item in (mentions or [])]
        clean_mentions = [item for item in clean_mentions if item]
        request_id = str(client_request_id or "").strip() or None
        if request_id and len(request_id) > 64:
            raise ValueError("client_request_id must be at most 64 characters")
        if len(clean_mentions) > MAX_SPACE_MENTIONS:
            raise ValueError(f"a Space comment may mention at most {MAX_SPACE_MENTIONS} characters")
        if len(clean_mentions) != len(set(clean_mentions)):
            raise ValueError("Space comment mentions must be unique")
        if normalized_actor_type == "CHARACTER" and clean_mentions:
            raise ValueError("character-authored comments cannot mention characters")
        if normalized_actor_type == "USER" and mentions_user:
            raise ValueError("user-authored comments cannot mention the user")
        if not clean_content and clean_sticker_id is None:
            raise ValueError("space comment requires content or sticker")
        if reply_to_comment_id is not None:
            parent = self.get_comment(int(reply_to_comment_id))
            if parent is None or int(parent.post_id) != int(post_id):
                raise ValueError("reply target does not belong to this post")

        with self.store.transaction():
            replay = self.find_comment_request_replay(
                post_id,
                character_id,
                clean_content,
                actor_type=normalized_actor_type,
                reply_to_comment_id=reply_to_comment_id,
                sticker_id=clean_sticker_id,
                mentions=clean_mentions,
                mentions_user=mentions_user,
                client_request_id=request_id,
            )
            if replay is not None:
                return replay
            if normalized_actor_type == "CHARACTER":
                existing = self.store.conn.execute(
                    "SELECT 1 FROM space_comments WHERE post_id=? AND character_id=? AND actor_type='CHARACTER' LIMIT 1",
                    (int(post_id), character_id),
                ).fetchone()
                if existing is None:
                    row = self.store.conn.execute(
                        "SELECT COUNT(DISTINCT character_id) AS total FROM space_comments "
                        "WHERE post_id=? AND actor_type='CHARACTER'",
                        (int(post_id),),
                    ).fetchone()
                    if int(row["total"] if row is not None else 0) >= MAX_COMMENTERS_PER_POST:
                        raise ValueError(f"a space post may have at most {MAX_COMMENTERS_PER_POST} character commenters")
            cur = self.store.conn.execute(
                "INSERT INTO space_comments("
                "post_id,character_id,actor_type,content,sticker_id,created_at,created_at_epoch,reply_to_comment_id,"
                "mentions_json,mentions_user,client_request_id"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    int(post_id),
                    character_id,
                    normalized_actor_type,
                    clean_content,
                    clean_sticker_id,
                    now.isoformat(),
                    epoch_us(now),
                    reply_to_comment_id,
                    json.dumps(clean_mentions, ensure_ascii=False, separators=(",", ":")),
                    int(bool(mentions_user)),
                    request_id,
                ),
            )
            reasons: list[str] = []
            if normalized_actor_type == "CHARACTER":
                if reply_to_comment_id is not None:
                    parent_row = self.store.conn.execute(
                        "SELECT actor_type FROM space_comments WHERE id=? AND post_id=?",
                        (int(reply_to_comment_id), int(post_id)),
                    ).fetchone()
                    if parent_row is not None and str(parent_row["actor_type"] or "CHARACTER") == "USER":
                        reasons.append("REPLY")
                if mentions_user:
                    reasons.append("MENTION")
            if reasons:
                self.store.conn.execute(
                    "INSERT OR IGNORE INTO space_notifications("
                    "recipient_id,post_id,comment_id,reasons_json,created_at,created_at_epoch"
                    ") VALUES('user',?,?,?,?,?)",
                    (
                        int(post_id),
                        int(cur.lastrowid),
                        json.dumps(reasons, separators=(",", ":")),
                        now.isoformat(),
                        epoch_us(now),
                    ),
                )
        return SpaceComment(
            id=int(cur.lastrowid),
            post_id=int(post_id),
            character_id=character_id,
            actor_type=normalized_actor_type,
            content=clean_content,
            sticker_id=clean_sticker_id,
            created_at=now,
            reply_to_comment_id=reply_to_comment_id,
            mentions=clean_mentions,
            mentions_user=bool(mentions_user),
        )

    def list_notifications(
        self,
        *,
        unread_only: bool = True,
        limit: int = 50,
        before_id: int | None = None,
        recipient_id: str = "user",
    ) -> list[SpaceNotification]:
        page_size = max(1, min(int(limit), 100))
        sql = "SELECT * FROM space_notifications WHERE recipient_id=?"
        args: list[Any] = [str(recipient_id)]
        if unread_only:
            sql += " AND read_at IS NULL"
        if before_id is not None:
            sql += " AND id<?"
            args.append(int(before_id))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(page_size)
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        return [self._notification_from_row(row) for row in rows]

    def unread_notification_count(self, *, recipient_id: str = "user") -> int:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS total FROM space_notifications "
                "WHERE recipient_id=? AND read_at IS NULL",
                (str(recipient_id),),
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def mark_notification_read(
        self,
        notification_id: int,
        now: datetime,
        *,
        recipient_id: str = "user",
    ) -> SpaceNotification | None:
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE space_notifications SET read_at=COALESCE(read_at,?),"
                "read_at_epoch=COALESCE(read_at_epoch,?) WHERE id=? AND recipient_id=?",
                (now.isoformat(), epoch_us(now), int(notification_id), str(recipient_id)),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute(
                "SELECT * FROM space_notifications WHERE id=? AND recipient_id=?",
                (int(notification_id), str(recipient_id)),
            ).fetchone()
        return self._notification_from_row(row) if row is not None else None

    def list_reactions(self, post_id: int, reaction_type: str = "LIKE") -> list[SpaceReaction]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM space_reactions WHERE post_id=? AND reaction_type=? "
                "ORDER BY created_at_epoch,character_id",
                (int(post_id), reaction_type),
            ).fetchall()
        return [self._reaction_from_row(row) for row in rows]

    def pair_engagement_counts(self, first_id: str, second_id: str) -> dict[str, int]:
        """Cheap durable Space affinity signal for audience selection.

        This intentionally reads only public Space facts that already exist:
        comments written on each other's posts and LIKE reactions on each
        other's posts.  It does not invoke a model and it does not infer a
        hidden relationship score from private chat.
        """
        first = str(first_id or "").strip()
        second = str(second_id or "").strip()
        if not first or not second or first == second:
            return {"comments": 0, "likes": 0}
        pair = (first, second, second, first)
        with self.store._lock:
            comments = self.store.conn.execute(
                "SELECT COUNT(*) AS total FROM space_comments c "
                "JOIN space_posts p ON p.id=c.post_id "
                "WHERE c.actor_type='CHARACTER' AND "
                "((c.character_id=? AND p.character_id=?) OR "
                "(c.character_id=? AND p.character_id=?))",
                pair,
            ).fetchone()
            likes = self.store.conn.execute(
                "SELECT COUNT(*) AS total FROM space_reactions r "
                "JOIN space_posts p ON p.id=r.post_id "
                "WHERE r.reaction_type='LIKE' AND "
                "((r.character_id=? AND p.character_id=?) OR "
                "(r.character_id=? AND p.character_id=?))",
                pair,
            ).fetchone()
        return {
            "comments": int(comments["total"] if comments is not None else 0),
            "likes": int(likes["total"] if likes is not None else 0),
        }

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


    def get_daily_run(self, character_id: str, local_date: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM space_daily_runs WHERE character_id=? AND local_date=?",
                (character_id, local_date),
            ).fetchone()
        return dict(row) if row is not None else None

    def claim_daily_run(
        self,
        character_id: str,
        local_date: str,
        scheduled_for: datetime,
        now: datetime,
    ) -> bool:
        """Atomically claim one character/date opportunity.

        The composite primary key makes the scheduler restart-safe. Dev/manual
        opportunities deliberately bypass this table so testing never consumes
        the real daily opportunity.
        """
        with self.store._lock:
            cur = self.store.conn.execute(
                "INSERT OR IGNORE INTO space_daily_runs("
                "character_id,local_date,scheduled_for,scheduled_for_epoch,"
                "started_at,started_at_epoch,status,error"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    character_id,
                    local_date,
                    scheduled_for.isoformat(),
                    epoch_us(scheduled_for),
                    now.isoformat(),
                    epoch_us(now),
                    "RUNNING",
                    "",
                ),
            )
            self.store._maybe_commit()
            return cur.rowcount > 0

    def finish_daily_run(
        self,
        character_id: str,
        local_date: str,
        now: datetime,
        *,
        status: str,
        post_id: int | None = None,
        error: str = "",
    ) -> None:
        normalized = str(status or "").strip().upper()
        if normalized not in {"POSTED", "NO_POST", "FAILED"}:
            raise ValueError("invalid Space daily run status")
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE space_daily_runs SET completed_at=?,completed_at_epoch=?,status=?,post_id=?,error=? "
                "WHERE character_id=? AND local_date=?",
                (
                    now.isoformat(),
                    epoch_us(now),
                    normalized,
                    post_id,
                    str(error or "")[:2000],
                    character_id,
                    local_date,
                ),
            )
            self.store._maybe_commit()

    def list_daily_runs(self, local_date: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM space_daily_runs"
        args: list[Any] = []
        if local_date:
            sql += " WHERE local_date=?"
            args.append(local_date)
        sql += " ORDER BY scheduled_for_epoch,character_id"
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]


    def get_opportunity_state(self, character_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM space_opportunity_state WHERE character_id=?",
                (character_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def ensure_opportunity_state(
        self,
        character_id: str,
        now: datetime,
        interval_minutes: float,
    ) -> dict[str, Any]:
        normalized_interval = max(10.0, float(interval_minutes))
        interval_seconds = normalized_interval * 60.0
        next_at = datetime.fromtimestamp(now.timestamp() + interval_seconds, tz=now.tzinfo)
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM space_opportunity_state WHERE character_id=?",
                (character_id,),
            ).fetchone()
            if row is None:
                self.store.conn.execute(
                    "INSERT INTO space_opportunity_state("
                    "character_id,next_opportunity_at,next_opportunity_at_epoch,"
                    "configured_interval_minutes,updated_at,updated_at_epoch"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        character_id,
                        next_at.isoformat(),
                        epoch_us(next_at),
                        normalized_interval,
                        now.isoformat(),
                        epoch_us(now),
                    ),
                )
            elif abs(float(row["configured_interval_minutes"] or 0.0) - normalized_interval) > 1e-9:
                # The durable cursor belonged to a different configured cadence.
                # Re-arm once from "now"; later restarts with the same interval
                # leave the cursor untouched, so frequent restarts cannot starve
                # the scheduler indefinitely.
                self.store.conn.execute(
                    "UPDATE space_opportunity_state SET "
                    "next_opportunity_at=?,next_opportunity_at_epoch=?,"
                    "configured_interval_minutes=?,updated_at=?,updated_at_epoch=? "
                    "WHERE character_id=?",
                    (
                        next_at.isoformat(),
                        epoch_us(next_at),
                        normalized_interval,
                        now.isoformat(),
                        epoch_us(now),
                        character_id,
                    ),
                )
            self.store._maybe_commit()
            row = self.store.conn.execute(
                "SELECT * FROM space_opportunity_state WHERE character_id=?",
                (character_id,),
            ).fetchone()
        return dict(row)

    def set_next_opportunity(
        self,
        character_id: str,
        next_at: datetime,
        now: datetime,
        *,
        interval_minutes: float | None = None,
    ) -> dict[str, Any]:
        normalized_interval = (
            max(10.0, float(interval_minutes)) if interval_minutes is not None else None
        )
        with self.store._lock:
            existing = self.store.conn.execute(
                "SELECT configured_interval_minutes FROM space_opportunity_state WHERE character_id=?",
                (character_id,),
            ).fetchone()
            stored_interval = (
                normalized_interval
                if normalized_interval is not None
                else float(existing["configured_interval_minutes"] or 0.0) if existing is not None else 0.0
            )
            self.store.conn.execute(
                "INSERT INTO space_opportunity_state("
                "character_id,next_opportunity_at,next_opportunity_at_epoch,"
                "configured_interval_minutes,updated_at,updated_at_epoch"
                ") VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(character_id) DO UPDATE SET "
                "next_opportunity_at=excluded.next_opportunity_at,"
                "next_opportunity_at_epoch=excluded.next_opportunity_at_epoch,"
                "configured_interval_minutes=excluded.configured_interval_minutes,"
                "updated_at=excluded.updated_at,updated_at_epoch=excluded.updated_at_epoch",
                (
                    character_id,
                    next_at.isoformat(),
                    epoch_us(next_at),
                    stored_interval,
                    now.isoformat(),
                    epoch_us(now),
                ),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute(
                "SELECT * FROM space_opportunity_state WHERE character_id=?",
                (character_id,),
            ).fetchone()
        return dict(row)

    def claim_due_opportunity(
        self,
        character_id: str,
        now: datetime,
        interval_minutes: float,
        *,
        source: str = "SCHEDULED",
    ) -> int | None:
        interval_seconds = max(600.0, float(interval_minutes) * 60.0)
        next_at = datetime.fromtimestamp(now.timestamp() + interval_seconds, tz=now.tzinfo)
        now_epoch = epoch_us(now)
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM space_opportunity_state WHERE character_id=?",
                (character_id,),
            ).fetchone()
            if row is None or int(row["next_opportunity_at_epoch"]) > now_epoch:
                return None
            cur = self.store.conn.execute(
                "INSERT INTO space_opportunity_runs("
                "character_id,scheduled_for,scheduled_for_epoch,started_at,started_at_epoch,status,source,error"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    character_id,
                    str(row["next_opportunity_at"]),
                    int(row["next_opportunity_at_epoch"]),
                    now.isoformat(),
                    now_epoch,
                    "RUNNING",
                    str(source or "SCHEDULED"),
                    "",
                ),
            )
            self.store.conn.execute(
                "UPDATE space_opportunity_state SET "
                "next_opportunity_at=?,next_opportunity_at_epoch=?,configured_interval_minutes=?,"
                "updated_at=?,updated_at_epoch=? WHERE character_id=?",
                (
                    next_at.isoformat(),
                    epoch_us(next_at),
                    max(10.0, float(interval_minutes)),
                    now.isoformat(),
                    now_epoch,
                    character_id,
                ),
            )
            self.store._maybe_commit()
            return int(cur.lastrowid)

    def finish_opportunity_run(
        self,
        run_id: int,
        character_id: str,
        now: datetime,
        *,
        status: str,
        post_id: int | None = None,
        error: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        normalized = str(status or "").strip().upper()
        if normalized not in {"POSTED", "NO_POST", "FAILED"}:
            raise ValueError("invalid Space opportunity run status")
        now_epoch = epoch_us(now)
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE space_opportunity_runs SET completed_at=?,completed_at_epoch=?,status=?,post_id=?,error=?,details_json=? "
                "WHERE id=? AND character_id=?",
                (
                    now.isoformat(),
                    now_epoch,
                    normalized,
                    post_id,
                    str(error or "")[:2000],
                    json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                    int(run_id),
                    character_id,
                ),
            )
            self.store.conn.execute(
                "UPDATE space_opportunity_state SET "
                "last_opportunity_at=?,last_opportunity_at_epoch=?,last_status=?,last_post_id=?,"
                "updated_at=?,updated_at_epoch=? WHERE character_id=?",
                (
                    now.isoformat(),
                    now_epoch,
                    normalized,
                    post_id,
                    now.isoformat(),
                    now_epoch,
                    character_id,
                ),
            )
            self.store._maybe_commit()

    @staticmethod
    def _opportunity_run_row(row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json", "{}") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["details"] = {}
        return item

    def list_opportunity_runs(
        self,
        *,
        character_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM space_opportunity_runs"
        args: list[Any] = []
        if character_id:
            sql += " WHERE character_id=?"
            args.append(character_id)
        sql += " ORDER BY started_at_epoch DESC,id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        return [self._opportunity_run_row(row) for row in rows]

    def get_opportunity_run(
        self,
        run_id: int,
        *,
        character_id: str | None = None,
    ) -> dict[str, Any] | None:
        """One run with its full details, including the raw model output.

        The scheduling status deliberately reports a summary of the ledger and
        points here instead, so reading one decision does not make every status
        poll carry every raw output.
        """
        sql = "SELECT * FROM space_opportunity_runs WHERE id=?"
        args: list[Any] = [int(run_id)]
        if character_id:
            sql += " AND character_id=?"
            args.append(character_id)
        with self.store._lock:
            row = self.store.conn.execute(sql, args).fetchone()
        return self._opportunity_run_row(row) if row is not None else None
