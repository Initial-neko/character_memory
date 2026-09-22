from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from pydantic import BaseModel, Field

from character_memory.time_utils import epoch_us, parse_datetime


MAX_SPACE_MEDIA_PER_POST = 9
SPACE_MEDIA_TYPES = {"IMAGE", "VOICE", "LINK_PREVIEW"}
SPACE_MEDIA_SOURCES = {"SEARCH", "GENERATED", "CHARACTER", "WEB", "LEGACY"}


class SpacePostMedia(BaseModel):
    post_id: int
    media_id: str = Field(min_length=1, max_length=120)
    media_type: str = Field(min_length=1, max_length=32)
    source_type: str = Field(min_length=1, max_length=32)
    sort_order: int = Field(ge=0, lt=MAX_SPACE_MEDIA_PER_POST)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SpacePostMediaRepository:
    """Ordered Space attachment relations backed by existing MediaAsset ids.

    Media bytes remain owned by MediaStorage/SQLiteStore. This table only says
    which durable assets belong to one Space post, in which order, and with
    which social-media semantics. Link previews can join the same relation later
    without creating a second Space post model.
    """

    def __init__(self, store):
        self.store = store
        self._init_schema()

    def _init_schema(self) -> None:
        now = datetime.now().astimezone()
        with self.store._lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS space_post_media(
                    post_id INTEGER NOT NULL,
                    media_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    PRIMARY KEY(post_id, sort_order),
                    UNIQUE(post_id, media_id)
                );

                CREATE INDEX IF NOT EXISTS idx_space_post_media_post
                    ON space_post_media(post_id,sort_order);
                CREATE INDEX IF NOT EXISTS idx_space_post_media_asset
                    ON space_post_media(media_id);
                """
            )
            # Gradual migration: existing single-media posts keep their legacy
            # space_posts.media_id field, while the new relation becomes the
            # canonical ordered read path.
            #
            # The kind is read from the asset's own mime type instead of being
            # assumed. A legacy attachment is not necessarily an image -- the
            # media store holds voice clips too -- and because the INSERT is
            # OR IGNORE against UNIQUE(post_id,media_id), a wrong guess here is
            # never revisited: the row is written once and skipped forever.
            # An asset that is missing or neither image nor audio still lands as
            # IMAGE, which is the only remaining guess SPACE_MEDIA_TYPES allows.
            self.store.conn.execute(
                """
                INSERT OR IGNORE INTO space_post_media(
                    post_id,media_id,media_type,source_type,sort_order,
                    metadata_json,created_at,created_at_epoch
                )
                SELECT p.id,p.media_id,
                       CASE WHEN LOWER(COALESCE(m.mime_type,'')) LIKE 'audio/%'
                            THEN 'VOICE' ELSE 'IMAGE' END,
                       'LEGACY',0,'{}',p.created_at,p.created_at_epoch
                FROM space_posts p
                LEFT JOIN media_assets m ON m.id=p.media_id
                WHERE p.media_id IS NOT NULL AND TRIM(p.media_id) <> ''
                """
            )
            # Repair rows a previous build already wrote as IMAGE. Without this
            # the OR IGNORE above leaves them wrong forever, since the unique
            # key means they are never reconsidered.
            self.store.conn.execute(
                """
                UPDATE space_post_media
                SET media_type='VOICE'
                WHERE source_type='LEGACY' AND media_type='IMAGE'
                  AND media_id IN (
                      SELECT id FROM media_assets
                      WHERE LOWER(COALESCE(mime_type,'')) LIKE 'audio/%'
                  )
                """
            )
            self.store._ensure_migration_table_locked()
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("space/004-post-media", now.isoformat()),
            )
            self.store._maybe_commit()

    @staticmethod
    def _normalize_media_type(value: str) -> str:
        normalized = str(value or "").strip().upper()
        if normalized not in SPACE_MEDIA_TYPES:
            raise ValueError(f"unsupported Space media type: {normalized or '<empty>'}")
        return normalized

    @staticmethod
    def _normalize_source_type(value: str) -> str:
        normalized = str(value or "").strip().upper()
        if normalized not in SPACE_MEDIA_SOURCES:
            raise ValueError(f"unsupported Space media source: {normalized or '<empty>'}")
        return normalized

    @staticmethod
    def _metadata_json(value: Any) -> str:
        metadata = value if isinstance(value, dict) else {}
        return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _from_row(row) -> SpacePostMedia:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return SpacePostMedia(
            post_id=int(row["post_id"]),
            media_id=str(row["media_id"]),
            media_type=str(row["media_type"]),
            source_type=str(row["source_type"]),
            sort_order=int(row["sort_order"]),
            metadata=metadata,
            created_at=parse_datetime(row["created_at"]),
        )

    def list_for_post(self, post_id: int) -> list[SpacePostMedia]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM space_post_media WHERE post_id=? ORDER BY sort_order",
                (int(post_id),),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def replace_for_post(
        self,
        post_id: int,
        items: list[dict[str, Any]],
        now: datetime,
    ) -> list[SpacePostMedia]:
        if len(items) > MAX_SPACE_MEDIA_PER_POST:
            raise ValueError(f"a Space post may contain at most {MAX_SPACE_MEDIA_PER_POST} media items")

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            media_id = str(item.get("media_id") or "").strip()
            if not media_id:
                raise ValueError("Space media_id must not be empty")
            if media_id in seen:
                raise ValueError("duplicate media_id in one Space post")
            seen.add(media_id)
            normalized.append(
                {
                    "media_id": media_id,
                    "media_type": self._normalize_media_type(item.get("media_type")),
                    "source_type": self._normalize_source_type(item.get("source_type")),
                    "sort_order": index,
                    "metadata_json": self._metadata_json(item.get("metadata")),
                }
            )

        with self.store._lock:
            post = self.store.conn.execute(
                "SELECT 1 FROM space_posts WHERE id=?",
                (int(post_id),),
            ).fetchone()
            if post is None:
                raise KeyError("space post not found")

            self.store.conn.execute(
                "DELETE FROM space_post_media WHERE post_id=?",
                (int(post_id),),
            )
            for item in normalized:
                self.store.conn.execute(
                    """
                    INSERT INTO space_post_media(
                        post_id,media_id,media_type,source_type,sort_order,
                        metadata_json,created_at,created_at_epoch
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(post_id),
                        item["media_id"],
                        item["media_type"],
                        item["source_type"],
                        item["sort_order"],
                        item["metadata_json"],
                        now.isoformat(),
                        epoch_us(now),
                    ),
                )
            self.store._maybe_commit()

        return self.list_for_post(post_id)
