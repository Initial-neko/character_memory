"""Bounded, read-only relationship review and sourced directional cognition.

The directional projection never creates relationship state. It reads the
character's own Memories and the durable facts those Memories point at, then
marks a Memory as this character's cognition of a peer only when the peer's
real message (or the actual witnessed group input recorded by a Trace) is
present. Corrections, forgetting, withdrawal and SIMULATED scope invalidate
the cognition; none of them delete the underlying Memory or Event.
"""
from datetime import datetime, timedelta, timezone
import json

from character_memory.character_life import obj, text
from character_memory.domain.experience_scope import is_real_experience
from character_memory.group_store import GroupRepository
from character_memory.storage.conversation_facts import CONVERSATION_FACT_ID_FLOOR
from character_memory.time_utils import epoch_us

MAX_COGNITION_MEMORIES = 400
MAX_COGNITION_ENTRIES = 50
MAX_COGNITION_TRACES = 600
MAX_RECENT_GROUP_EVENTS = 14
MAX_SHARED_HISTORY_EVENTS = 4
COGNITION_BOUNDARIES = (
    ("OWN_MEMORY_ONLY", "只展示该人物自己保存、且能回溯到真实事实的记忆；没有保存的内心想法不展示。"),
    ("DIRECTIONAL", "A 对 B 与 B 对 A 分开计算，不能从一方推断另一方。"),
    ("WITNESSED_ONLY", "同群不等于互动；只有该人物实际看到对方的真实消息或评论，才作为依据。"),
    ("INVALIDATION", "记忆或来源被更正、忘记、撤回或标为 SIMULATED 后，不再作为当前认知断言。"),
    ("NO_INFERENCE", "这里不推断好感、关系等级、隐藏态度或对方未表达的想法。"),
)


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
            # Migrated conversation facts are excluded from this legacy scan and
            # read from their DIRECT conversation below, so one message is never
            # shown twice and never disappears after the migration.
            direct = f"""SELECT e.* FROM events e WHERE e.character_id=?
                AND e.event_time_epoch>=? AND e.event_time_epoch<?
                AND julianday(e.event_time) IS NOT NULL
                AND NOT (e.event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE') AND EXISTS (
                    SELECT 1 FROM conversation_fact_aliases a WHERE a.legacy_kind='EVENT'
                    AND a.legacy_id=e.id AND a.status='MIGRATED'))
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
            if {"conversation_events", "conversations", "conversation_members"} <= tables:
                chat = """SELECT e.*,cm.actor_id AS character_id FROM conversation_events e
                    JOIN conversations dc ON dc.id=e.conversation_id AND dc.type='DIRECT'
                    JOIN conversation_members cm ON cm.conversation_id=e.conversation_id
                        AND cm.actor_type='CHARACTER' AND cm.actor_id=?
                    WHERE e.event_time_epoch>=? AND e.event_time_epoch<?
                    AND julianday(e.event_time) IS NOT NULL
                    AND e.event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')
                    AND ((?='user' AND e.actor_type='USER' AND e.actor_id='user')
                      OR e.actor_id=?)
                    ORDER BY e.event_time_epoch DESC,e.id DESC LIMIT 11"""
                for row in self.store.conn.execute(chat, [character_id, *bounds, peer, peer]):
                    # Physical table ≠ domain: Direct facts keep the EVENT kind and
                    # the existing `events:<id>` source key (ADR §5.1).
                    interactions.append(self._fact("events", row, "DIRECT", row["event_time"], row["event_time_epoch"]))
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
                # An intent's source may be a legacy prompt input below the unified
                # floor or a Direct conversation fact above it. Both domains are
                # read here; a LEFT JOIN to `events` alone would drop every intent
                # whose source is now a unified row.
                direct_fact_sql = (
                    "SELECT ce.id,cm.actor_id AS character_id,ce.event_type,ce.event_time,"
                    "ce.event_time_epoch,ce.metadata_json FROM conversation_events ce "
                    "JOIN conversations c ON c.id=ce.conversation_id AND c.type='DIRECT' "
                    "JOIN conversation_members cm ON cm.conversation_id=ce.conversation_id "
                    "AND cm.actor_type='CHARACTER'"
                ) if {"conversation_events", "conversations", "conversation_members"} <= tables else None
                source_meta = "CASE WHEN json_valid(s.metadata_json) THEN s.metadata_json ELSE '{}' END"
                if direct_fact_sql is not None:
                    plans = f"""SELECT i.*,s.metadata_json AS source_metadata FROM intents i
                        LEFT JOIN ({direct_fact_sql} UNION ALL SELECT id,character_id,event_type,event_time,event_time_epoch,metadata_json FROM events) s
                            ON s.id=i.source_event_id
                        WHERE i.character_id=? AND i.status='PENDING' AND i.preferred_action='PROACTIVE_MESSAGE'
                        AND i.created_at_epoch<=? AND i.earliest_at_epoch IS NOT NULL AND i.expires_at_epoch>=?
                        AND julianday(i.created_at) IS NOT NULL AND julianday(i.earliest_at) IS NOT NULL AND julianday(i.expires_at) IS NOT NULL
                        AND i.earliest_at_epoch<=i.expires_at_epoch
                        AND (i.source_event_id IS NULL OR (s.character_id=i.character_id
                            AND s.event_type IN ('USER_MESSAGE','TIME_TICK') AND s.event_time_epoch<=i.created_at_epoch AND julianday(s.event_time) IS NOT NULL
                            AND upper(coalesce(json_extract({source_meta},'$.channel'),'')) IN ('','DIRECT')
                            AND upper(coalesce(json_extract({source_meta},'$.origin'),''))<>'GROUP'
                            AND json_extract({source_meta},'$.source_conversation_event_id') IS NULL
                            AND json_extract({source_meta},'$.group_event_id') IS NULL))
                        ORDER BY i.earliest_at_epoch,i.id LIMIT 6"""
                else:
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
            cognition = self._cognition_locked(character_id, peer, tables, now)
            cognition["open_items"] = [{
                "kind": "PENDING_PROACTIVE_INTENT", "content": item["content"],
                "earliest_at": item["earliest_at"], "expires_at": item["expires_at"],
                "evidence": {"kind": "intent", "id": item["source"]["id"],
                             "event_id": item["source"]["event_id"]},
                "source_known": item["source_known"],
            } for item in pending[:5]]
            cognition["open_items_has_more"] = len(pending) > 5
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
            "cognition": cognition,
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

    def _direct_source_row_locked(self, character_id, source_id):
        """Resolve a Memory's Direct source across both id spaces.

        Migrated Direct facts live in ``conversation_events`` above the unified
        floor; unmigrated rows and non-conversation inputs stay in ``events``.
        Ownership is checked here, not inferred from the number.
        """
        canonical = None
        try:
            canonical = self.store.fact_reader.canonical_id_for_legacy(int(source_id)) if source_id < CONVERSATION_FACT_ID_FLOOR else int(source_id)
        except (TypeError, ValueError, AttributeError):
            canonical = None
        if canonical is None:
            return self.store.conn.execute(
                "SELECT id,character_id,event_type,event_time,event_time_epoch,content,metadata_json "
                "FROM events WHERE id=? AND character_id=?",
                (source_id, character_id),
            ).fetchone()
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT e.*,cm.actor_id AS character_id FROM conversation_events e "
                "JOIN conversations c ON c.id=e.conversation_id AND c.type='DIRECT' "
                "JOIN conversation_members cm ON cm.conversation_id=e.conversation_id "
                "AND cm.actor_type='CHARACTER' AND cm.actor_id=? WHERE e.id=?",
                (character_id, canonical),
            ).fetchone()
        return row

    # --- directional cognition ------------------------------------------------

    def _cognition_locked(self, character_id, peer, tables, now):
        """Derive A→B cognition from A's own Memories and peer-authored facts."""
        rows = self.store.conn.execute(
            "SELECT * FROM memories WHERE character_id=? AND event_time_epoch IS NOT NULL "
            "ORDER BY event_time_epoch DESC,id DESC LIMIT ?",
            [character_id, MAX_COGNITION_MEMORIES + 1],
        ).fetchall()
        truncated = len(rows) > MAX_COGNITION_MEMORIES
        rows = rows[:MAX_COGNITION_MEMORIES]
        group_memory_ids = [
            int(row["id"]) for row in rows
            if str(obj(row["metadata_json"]).get("origin") or "").upper() == "GROUP"
        ]
        # A correction creates a new Memory with the same provenance. The trace
        # still names the version it created, so include the corrected-from IDs
        # when resolving witnessed input; the active replacement inherits it.
        for row in rows:
            if str(obj(row["metadata_json"]).get("origin") or "").upper() != "GROUP":
                continue
            predecessor = obj(row["metadata_json"]).get("corrects_memory_id")
            if type(predecessor) is int and predecessor not in group_memory_ids:
                group_memory_ids.append(predecessor)
        trace_links = self._trace_links_locked(character_id, group_memory_ids, tables)
        entries, history = [], []
        counts = {
            "memories_scanned": len(rows), "active": 0, "invalidated": 0,
            "superseded": 0, "forgotten": 0, "simulated_excluded": 0,
            "unattributed": 0, "truncated": truncated,
        }
        for row in rows:
            outcome = self._cognition_entry_locked(row, character_id, peer, tables, trace_links, counts)
            if outcome is None:
                continue
            entry, change = outcome
            entries.append(entry)
            history.extend(change)
        total_entries = len(entries)
        entries.sort(key=lambda item: (item["memory"]["event_time_epoch"] or 0, item["memory"]["id"]), reverse=True)
        # Counts describe every attributed Memory the scan found; only the
        # displayed list is bounded, so a truncated view never understates.
        for entry in entries:
            counts[entry["status"].lower()] = counts.get(entry["status"].lower(), 0) + 1
        entries = entries[:MAX_COGNITION_ENTRIES]
        if total_entries > MAX_COGNITION_ENTRIES:
            counts["truncated"] = True
        history.sort(key=lambda item: item["_stamp"] if item["_stamp"] is not None else -1, reverse=True)
        for item in history:
            item.pop("_stamp", None)
        boundaries = [{"code": code, "text": value} for code, value in COGNITION_BOUNDARIES]
        if counts["simulated_excluded"]:
            boundaries.append({"code": "SIMULATED_EXCLUDED",
                "text": f"另有 {counts['simulated_excluded']} 条记忆或其来源属于 SIMULATED，未进入真实关系认知。"})
        if counts["invalidated"]:
            boundaries.append({"code": "INVALIDATED_PRESENT",
                "text": f"其中 {counts['invalidated']} 条认知因来源失效而只保留为历史，不再作为当前断言。"})
        if counts["unattributed"]:
            boundaries.append({"code": "UNATTRIBUTED_EXCLUDED",
                "text": f"另有 {counts['unattributed']} 条记忆无法确认与该对象的真实消息关联，未计入。"})
        return {
            "direction": f"{character_id}->{peer}", "character_id": character_id, "peer": peer,
            "read_only": True, "asserted_count": counts["active"],
            "cognitions": entries, "change_history": history[:MAX_COGNITION_ENTRIES],
            "boundaries": boundaries, "counts": counts, "checked_at": now.isoformat(),
        }

    def _cognition_entry_locked(self, row, character_id, peer, tables, trace_links, counts):
        memory_id = int(row["id"])
        meta = obj(row["metadata_json"])
        if not is_real_experience(meta):
            counts["simulated_excluded"] += 1
            return None
        origin_group = str(meta.get("origin") or "").upper() == "GROUP"
        fact, source_state = self._source_fact_locked(row, meta, origin_group, tables)
        if source_state == "SIMULATED":
            counts["simulated_excluded"] += 1
            return None
        direct = source_state in ("REAL", "WITHDRAWN") and self._fact_authors_peer(fact, peer)
        witnesses, withdrawn = [], []
        if origin_group:
            link = trace_links.get(memory_id)
            predecessor = meta.get("corrects_memory_id")
            seen = {memory_id}
            while link is None and type(predecessor) is int and predecessor not in seen:
                seen.add(predecessor)
                link = trace_links.get(predecessor)
                if link is None:
                    previous = self.store.conn.execute(
                        "SELECT metadata_json FROM memories WHERE id=?", (predecessor,)
                    ).fetchone()
                    predecessor = obj(previous["metadata_json"]).get("corrects_memory_id") if previous is not None else None
            witnesses, withdrawn = self._trace_witnesses_locked(link, peer, tables)
        attributed = direct or bool(witnesses) or bool(withdrawn)
        if not attributed:
            counts["unattributed"] += 1
            return None
        if row["superseded_by"] is not None:
            status, reason = "SUPERSEDED", "MEMORY_CORRECTED"
        elif not bool(row["active"]):
            status, reason = "FORGOTTEN", "MEMORY_FORGOTTEN"
        elif source_state != "REAL":
            status, reason = "INVALIDATED", {"MISSING": "SOURCE_MISSING",
                "WITHDRAWN": "SOURCE_WITHDRAWN", "INVALID": "SOURCE_INVALID"}.get(source_state, "SOURCE_INVALID")
        elif origin_group and not direct and not witnesses and withdrawn:
            status, reason = "INVALIDATED", "SOURCE_WITHDRAWN"
        else:
            status, reason = "ACTIVE", None
        evidence = []
        if fact is not None and source_state in ("REAL", "WITHDRAWN") and direct:
            evidence.append({**self._fact_payload(fact),
                "role": "DIRECT_SOURCE" if source_state == "REAL" else "WITHDRAWN_INPUT"})
        for witness in witnesses:
            evidence.append({**self._fact_payload(witness), "role": "WITNESSED_INPUT"})
        for witness in withdrawn:
            evidence.append({**self._fact_payload(witness), "role": "WITHDRAWN_INPUT"})
        entry = {
            "memory": {
                "id": memory_id, "content": text(row["content"], 2000),
                "memory_type": text(row["memory_type"], 40), "importance": row["importance"],
                "event_time": row["event_time"], "event_time_epoch": row["event_time_epoch"],
                "active": bool(row["active"]), "superseded_by": row["superseded_by"],
            },
            "status": status, "reason": reason, "asserted": status == "ACTIVE",
            "attribution": "DIRECT_SOURCE" if direct else "TRACE_INPUT" if witnesses else "WITHDRAWN_INPUT",
            "evidence": evidence,
        }
        change = [{
            "kind": "MEMORY_CREATED", "memory_id": memory_id, "at": row["event_time"],
            "_stamp": row["event_time_epoch"], "status": status,
        }]
        if row["superseded_by"] is not None:
            replacement = self.store.conn.execute(
                "SELECT id,content,event_time,event_time_epoch,metadata_json FROM memories WHERE id=?",
                (int(row["superseded_by"]),),
            ).fetchone()
            corrected_at, corrected_stamp = None, None
            if replacement is not None:
                corrected_at = obj(replacement["metadata_json"]).get("corrected_at")
                if isinstance(corrected_at, str):
                    try:
                        corrected_stamp = epoch_us(datetime.fromisoformat(corrected_at))
                    except ValueError:
                        corrected_at = None
            change.append({
                "kind": "MEMORY_CORRECTED", "memory_id": memory_id, "at": corrected_at,
                "to_memory_id": int(row["superseded_by"]),
                "to_content": text(replacement["content"], 2000) if replacement is not None else None,
                "_stamp": corrected_stamp,
            })
        elif not bool(row["active"]):
            change.append({
                "kind": "MEMORY_FORGOTTEN", "memory_id": memory_id, "at": None,
                "_stamp": None,
            })
        return entry, change

    def _source_fact_locked(self, row, meta, origin_group, tables):
        if origin_group:
            if "conversation_events" not in tables:
                return None, "MISSING"
            conversation_id = meta.get("conversation_id")
            source_id = meta.get("source_conversation_event_id")
            if not isinstance(conversation_id, str) or type(source_id) is not int:
                return None, "INVALID"
            fact = self._group_event_locked(conversation_id, source_id)
            if fact is None:
                return None, "MISSING"
            if not is_real_experience(fact["metadata"]):
                return None, "SIMULATED"
            if bool(fact["metadata"].get("hidden")) and fact["event"].event_type in ("USER_MESSAGE", "CHARACTER_MESSAGE"):
                return fact, "WITHDRAWN"
            return fact, "REAL"
        if "source_conversation_event_id" in meta:
            return None, "INVALID"
        source_id = row["source_event_id"]
        if source_id is None:
            return None, "MISSING"
        if type(source_id) is not int:
            return None, "INVALID"
        event = self._direct_source_row_locked(row["character_id"], source_id)
        if event is None:
            return None, "MISSING"
        event_meta = obj(event["metadata_json"])
        if not is_real_experience(event_meta):
            return None, "SIMULATED"
        fact = {"kind": "EVENT", "row": event, "metadata": event_meta}
        if bool(event_meta.get("hidden")) and event["event_type"] in (
                "USER_MESSAGE", "CHARACTER_MESSAGE", "SPACE_COMMENT_RECEIVED"):
            return fact, "WITHDRAWN"
        return fact, "REAL"

    def _group_event_locked(self, conversation_id, event_id):
        row = self.store.conn.execute(
            "SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
            "WHERE e.id=? AND e.conversation_id=? AND c.type='GROUP'",
            (event_id, conversation_id),
        ).fetchone()
        if row is None:
            return None
        try:
            event = GroupRepository._event_from_row(row)
        except (ValueError, TypeError, KeyError):
            return None
        return {
            "kind": "CONVERSATION_EVENT", "row": row, "event": event,
            "metadata": event.metadata, "conversation_id": conversation_id,
        }

    @staticmethod
    def _fact_authors_peer(fact, peer):
        if fact is None:
            return False
        if fact["kind"] == "CONVERSATION_EVENT":
            event = fact["event"]
            if peer == "user":
                return event.actor_type == "USER" and event.event_type == "USER_MESSAGE"
            return (event.actor_type == "CHARACTER" and event.actor_id == peer
                    and event.event_type == "CHARACTER_MESSAGE")
        row, meta = fact["row"], fact["metadata"]
        if peer == "user":
            if row["event_type"] != "USER_MESSAGE":
                return False
            channel = str(meta.get("channel") or "").upper()
            origin = str(meta.get("origin") or "").upper()
            return (channel in ("", "DIRECT") and origin != "GROUP"
                    and meta.get("source_conversation_event_id") is None
                    and meta.get("group_event_id") is None)
        return (row["event_type"] == "SPACE_COMMENT_RECEIVED"
                and meta.get("commenter_id") == peer)

    @staticmethod
    def _fact_payload(fact):
        if fact["kind"] == "CONVERSATION_EVENT":
            event = fact["event"]
            return {
                "kind": "CONVERSATION_EVENT", "id": event.id,
                "conversation_id": event.conversation_id, "event_type": event.event_type,
                "actor_type": event.actor_type, "actor_id": event.actor_id,
                "time": event.event_time.isoformat(), "content": text(event.content, 300),
            }
        row = fact["row"]
        actor_type = "USER" if row["event_type"] == "USER_MESSAGE" else "CHARACTER" if row["event_type"] == "CHARACTER_MESSAGE" else "UNKNOWN"
        return {
            "kind": "EVENT", "id": row["id"], "event_type": row["event_type"],
            "actor_type": actor_type,
            "actor_id": "user" if row["event_type"] == "USER_MESSAGE" else fact["metadata"].get("commenter_id"),
            "time": row["event_time"], "content": text(row["content"], 300),
        }

    def _trace_links_locked(self, character_id, memory_ids, tables):
        if not memory_ids or "conversation_runtime_traces" not in tables:
            return {}
        links = {}
        for start in range(0, len(memory_ids), 300):
            chunk = memory_ids[start:start + 300]
            marks = ",".join("?" for _ in chunk)
            rows = self.store.conn.execute(
                f"""SELECT t.conversation_id,t.source_conversation_event_id,t.trace_json,t.created_at_epoch,
                           j.value AS memory_id
                    FROM conversation_runtime_traces t,
                         json_each(CASE WHEN json_valid(t.trace_json) THEN t.trace_json ELSE '{{}}' END,'$.created_memory_ids') j
                    WHERE t.character_id=? AND j.type='integer' AND j.value IN ({marks})
                    ORDER BY t.created_at_epoch DESC,t.id DESC LIMIT ?""",
                [character_id, *chunk, MAX_COGNITION_TRACES],
            ).fetchall()
            for row in rows:
                links.setdefault(int(row["memory_id"]), row)
        return links

    def _trace_witnesses_locked(self, link, peer, tables):
        if link is None or "conversation_events" not in tables:
            return [], []
        trace = obj(link["trace_json"])
        if not isinstance(trace, dict):
            return [], []
        conversation_id = link["conversation_id"]
        ids = []
        raw_recent = trace.get("recent_group_event_ids")
        if isinstance(raw_recent, list):
            ids.extend(value for value in raw_recent[:MAX_RECENT_GROUP_EVENTS] if type(value) is int)
        history = trace.get("shared_history_retrieval")
        raw_history = history.get("events") if isinstance(history, dict) else None
        if isinstance(raw_history, list):
            for item in raw_history[:MAX_SHARED_HISTORY_EVENTS]:
                if not isinstance(item, dict):
                    continue
                if item.get("conversation_id") != conversation_id:
                    continue
                if type(item.get("source_conversation_event_id")) is int:
                    ids.append(item["source_conversation_event_id"])
        witnesses, withdrawn = [], []
        seen = set()
        for event_id in ids:
            if event_id in seen:
                continue
            seen.add(event_id)
            fact = self._group_event_locked(conversation_id, event_id)
            if fact is None:
                continue
            event, meta = fact["event"], fact["metadata"]
            if event.event_type not in ("USER_MESSAGE", "CHARACTER_MESSAGE"):
                continue
            if not is_real_experience(meta):
                continue
            if not self._fact_authors_peer(fact, peer):
                continue
            (withdrawn if bool(meta.get("hidden")) else witnesses).append(fact)
        return witnesses, withdrawn
