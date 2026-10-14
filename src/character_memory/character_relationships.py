"""Bounded, read-only relationship review over existing facts and Intents."""
from datetime import datetime, timedelta, timezone

from character_memory.character_life import obj, text
from character_memory.time_utils import epoch_us


class CharacterRelationshipReader:
    def __init__(self, store):
        self.store = store

    def review(self, character_id, peer, *, day=None, offset=480, now=None):
        if not peer or peer == character_id or not -840 <= offset <= 840:
            raise ValueError("请选择另一个互动对象")
        now = now or datetime.now(timezone.utc)
        zone = timezone(timedelta(minutes=offset))
        selected = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone) if day else now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = selected - timedelta(days=6), min(selected + timedelta(days=1), now)
        start = min(start, end)
        bounds = [epoch_us(start), epoch_us(end)]
        interactions = []
        with self.store._lock:
            tables = {row[0] for row in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            metadata = "CASE WHEN json_valid(e.metadata_json) THEN e.metadata_json ELSE '{}' END"
            # Direct messages belong to this person and the User; social receipts
            # name their actual commenter, never a guessed name in message text.
            direct = f"""SELECT e.* FROM events e WHERE e.character_id=?
                AND e.event_time_epoch>=? AND e.event_time_epoch<?
                AND julianday(e.event_time) IS NOT NULL
                AND ((?='user' AND e.event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')
                    AND upper(coalesce(json_extract({metadata},'$.channel'),'')) IN ('','DIRECT')
                    AND upper(coalesce(json_extract({metadata},'$.origin'),''))<>'GROUP'
                    AND json_extract({metadata},'$.source_conversation_event_id') IS NULL
                    AND json_extract({metadata},'$.group_event_id') IS NULL)
                  OR (e.event_type='SPACE_COMMENT_RECEIVED' AND json_extract({metadata},'$.commenter_id')=?))
                ORDER BY e.event_time_epoch DESC,e.id DESC LIMIT 11"""
            for row in self.store.conn.execute(direct, [character_id, *bounds, peer, peer]):
                kind = "SPACE" if row["event_type"] == "SPACE_COMMENT_RECEIVED" else "DIRECT"
                interactions.append(self._fact("events", row, kind, row["event_time"], row["event_time_epoch"]))
            if {"conversation_events", "conversation_runtime_traces", "conversation_members"} <= tables:
                trace = "CASE WHEN json_valid(t.trace_json) THEN t.trace_json ELSE '{}' END"
                recent_ids = f"CASE WHEN json_type({trace},'$.recent_group_event_ids')='array' THEN json_extract({trace},'$.recent_group_event_ids') ELSE '[]' END"
                history = f"CASE WHEN json_type({trace},'$.shared_history_retrieval.events')='array' THEN json_extract({trace},'$.shared_history_retrieval.events') ELSE '[]' END"
                history_item = "CASE WHEN h.type='object' THEN h.value ELSE '{}' END"
                group = f"""SELECT e.* FROM conversation_events e
                    WHERE e.actor_id=? AND e.actor_type IN ('USER','CHARACTER') AND e.actor_id<>?
                    AND e.event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')
                    AND e.event_time_epoch>=? AND e.event_time_epoch<? AND julianday(e.event_time) IS NOT NULL
                    AND coalesce(json_extract({metadata},'$.hidden'),0)=0
                    AND EXISTS(SELECT 1 FROM conversation_members m WHERE m.conversation_id=e.conversation_id
                        AND m.actor_type='CHARACTER' AND m.actor_id=? AND m.joined_at_epoch<=e.event_time_epoch)
                    AND EXISTS(SELECT 1 FROM conversation_runtime_traces t WHERE t.conversation_id=e.conversation_id
                        AND t.character_id=? AND (t.source_conversation_event_id=e.id
                            OR EXISTS(SELECT 1 FROM json_each({recent_ids}) r
                                WHERE r.key<14 AND r.type='integer' AND r.value=e.id)
                            OR EXISTS(SELECT 1 FROM json_each({history}) h
                                WHERE h.key<4 AND h.type='object'
                                AND json_type({history_item},'$.source_conversation_event_id')='integer'
                                AND json_extract({history_item},'$.source_conversation_event_id')=e.id
                                AND json_extract({history_item},'$.conversation_id')=e.conversation_id))
                        AND julianday(t.created_at) IS NOT NULL
                        AND t.created_at_epoch>=e.event_time_epoch AND t.created_at_epoch<?)
                    ORDER BY e.event_time_epoch DESC,e.id DESC LIMIT 11"""
                for row in self.store.conn.execute(group, [peer, character_id, *bounds, character_id, character_id, epoch_us(end)]):
                    interactions.append(self._fact("group", row, "GROUP", row["event_time"], row["event_time_epoch"]))
            if {"space_comments", "space_posts"} <= tables:
                comments = """SELECT e.* FROM space_comments e JOIN space_posts p ON p.id=e.post_id
                    LEFT JOIN space_comments parent ON parent.id=e.reply_to_comment_id AND parent.post_id=e.post_id
                    WHERE e.actor_type='CHARACTER' AND e.character_id=?
                    AND (CASE WHEN e.reply_to_comment_id IS NULL THEN p.character_id ELSE parent.character_id END)=?
                    AND e.created_at_epoch>=? AND e.created_at_epoch<? AND julianday(e.created_at) IS NOT NULL
                    AND p.visibility='ACTIVE_CHARACTERS'
                    ORDER BY e.created_at_epoch DESC,e.id DESC LIMIT 11"""
                for row in self.store.conn.execute(comments, [character_id, peer, *bounds]):
                    interactions.append(self._fact("comment", row, "SPACE", row["created_at"], row["created_at_epoch"]))
            pending = []
            if peer == "user" and "intents" in tables:
                plans = f"""SELECT i.*,e.metadata_json AS source_metadata FROM intents i
                    LEFT JOIN events e ON e.id=i.source_event_id
                    WHERE i.character_id=? AND i.status='PENDING' AND i.preferred_action='PROACTIVE_MESSAGE'
                    AND i.created_at_epoch<=? AND i.earliest_at_epoch IS NOT NULL AND i.expires_at_epoch>=?
                    AND julianday(i.created_at) IS NOT NULL AND julianday(i.earliest_at) IS NOT NULL AND julianday(i.expires_at) IS NOT NULL
                    AND i.earliest_at_epoch<=i.expires_at_epoch
                    AND (i.source_event_id IS NULL OR (e.character_id=i.character_id
                        AND e.event_type IN ('USER_MESSAGE','TIME_TICK') AND e.event_time_epoch<=i.created_at_epoch AND julianday(e.event_time) IS NOT NULL
                        AND upper(coalesce(json_extract({metadata},'$.channel'),'')) IN ('','DIRECT')
                        AND upper(coalesce(json_extract({metadata},'$.origin'),''))<>'GROUP'
                        AND json_extract({metadata},'$.source_conversation_event_id') IS NULL
                        AND json_extract({metadata},'$.group_event_id') IS NULL))
                    ORDER BY i.earliest_at_epoch,i.id LIMIT 6"""
                for row in self.store.conn.execute(plans, [character_id, epoch_us(now), epoch_us(now)]):
                    pending.append({
                        "content": text(row["content"], 500), "status": "PENDING",
                        "earliest_at": row["earliest_at"], "expires_at": row["expires_at"],
                        "source": {"kind": "intent", "id": row["id"], "event_id": row["source_event_id"]},
                        "source_known": row["source_event_id"] is not None,
                    })
        interactions.sort(key=lambda item: (item["_stamp"], item["key"]), reverse=True)
        facts = interactions[:10]
        for item in facts:
            item.pop("_stamp")
        return {
            "character_id": character_id, "peer": peer, "read_only": True,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": 7},
            "interactions": facts, "interactions_has_more": len(interactions) > 10,
            "pending_intents": pending[:5], "pending_has_more": len(pending) > 5,
            "pending_scope": "CURRENT_USER_DIRECT_PENDING_ALL_CREATION_DATES",
            "checked_at": now.isoformat(),
        }

    @staticmethod
    def _fact(kind, row, channel, time, stamp):
        metadata = obj(row["metadata_json"]) if "metadata_json" in row.keys() else {}
        return {
            "key": f"{kind}:{row['id']}", "time": time, "channel": channel,
            "content": text(metadata.get("display_text") or row["content"], 500), "_stamp": stamp,
            "source": {"kind": kind, "id": row["id"],
                       "conversation_id": row["conversation_id"] if "conversation_id" in row.keys() else metadata.get("conversation_id")},
        }
