"""Conservative durable reservations for optional autonomous Direct images."""
from datetime import timedelta, timezone

from character_memory.time_utils import aware_datetime, epoch_us


class ImageGenerationJobs:
    def __init__(self, store, *, boot_id):
        self.store, self.boot_id = store, boot_id
        def migrate():
            store.conn.execute("""CREATE TABLE IF NOT EXISTS direct_image_generation_jobs(
                id INTEGER PRIMARY KEY,character_id TEXT NOT NULL,source_event_id INTEGER NOT NULL,
                action_index INTEGER NOT NULL,request TEXT NOT NULL,boot_id TEXT NOT NULL,
                status TEXT NOT NULL,created_at TEXT NOT NULL,created_at_epoch INTEGER NOT NULL,
                event_id INTEGER,error TEXT NOT NULL DEFAULT '',
                UNIQUE(character_id,source_event_id,action_index))""")
            store.conn.execute("CREATE INDEX IF NOT EXISTS idx_direct_image_jobs_time ON direct_image_generation_jobs(created_at_epoch,character_id)")
        store.apply_schema_migration("visual/001-direct-image-generation-jobs", migrate, immediate=True)
        with store.transaction(immediate=True):
            # Single application process recovery, never a distributed lease.
            # No external retry: a prior worker may have paid before interruption.
            store.conn.execute("UPDATE direct_image_generation_jobs SET status='INTERRUPTED' WHERE status='RUNNING' AND boot_id<>?", [boot_id])

    def _denial(self, character_id, now, daily_max, cooldown_hours):
        if daily_max <= 0:
            return "DAILY_LIMIT"
        now = aware_datetime(now).astimezone(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        used = self.store.conn.execute("SELECT COUNT(*) FROM direct_image_generation_jobs WHERE created_at_epoch>=? AND created_at_epoch<?", [epoch_us(start), epoch_us(start + timedelta(days=1))]).fetchone()[0]
        if used >= daily_max:
            return "DAILY_LIMIT"
        latest = self.store.conn.execute("SELECT MAX(created_at_epoch) FROM direct_image_generation_jobs WHERE character_id=?", [character_id]).fetchone()[0]
        if latest is not None and epoch_us(now) < latest + int(cooldown_hours * 3600 * 1_000_000):
            return "COOLDOWN"
        if self.store.conn.execute("SELECT 1 FROM direct_image_generation_jobs WHERE status='RUNNING' LIMIT 1").fetchone():
            return "BUSY"
        return ""

    def check(self, character_id, *, now, daily_max, cooldown_hours):
        with self.store._lock:
            return self._denial(character_id, now, daily_max, cooldown_hours)

    def reserve(self, *, character_id, source_event_id, action_index, request, now, daily_max, cooldown_hours):
        now = aware_datetime(now).astimezone(timezone.utc)
        with self.store.transaction(immediate=True):
            existing = self.store.conn.execute("SELECT request FROM direct_image_generation_jobs WHERE character_id=? AND source_event_id=? AND action_index=?", [character_id, source_event_id, action_index]).fetchone()
            if existing is not None:
                return None, "DUPLICATE" if existing[0] == request else "REQUEST_CONFLICT"
            denial = self._denial(character_id, now, daily_max, cooldown_hours)
            if denial:
                return None, denial
            row = self.store.conn.execute("INSERT INTO direct_image_generation_jobs(character_id,source_event_id,action_index,request,boot_id,status,created_at,created_at_epoch) VALUES(?,?,?,?,?,'RUNNING',?,?)", [character_id, source_event_id, action_index, request, self.boot_id, now.isoformat(), epoch_us(now)])
            return int(row.lastrowid), ""

    def finish(self, job_id, *, status, event_id=None, error=""):
        with self.store._lock:
            row = self.store.conn.execute("UPDATE direct_image_generation_jobs SET status=?,event_id=?,error=? WHERE id=? AND boot_id=? AND status='RUNNING'", [status, event_id, str(error)[:500], job_id, self.boot_id])
            self.store._maybe_commit()
            return row.rowcount == 1
