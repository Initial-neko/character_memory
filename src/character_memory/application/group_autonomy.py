from __future__ import annotations

from datetime import datetime, timedelta
import logging
import threading
from uuid import uuid4

from character_memory.application.async_conversation import group_channel
from character_memory.application.group_conversation_service import (
    GroupConversationService,
    SupersededGroupReaction,
)
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.time_utils import epoch_us


logger = logging.getLogger("character_memory.application.group_autonomy")


def autonomy_enabled(access) -> bool:
    return bool(
        getattr(access.settings, "api_key", "")
        and getattr(access.settings, "group_autonomy_enabled", True)
    )


class GroupAutonomyService:
    """One bounded autonomous opportunity for an existing shared group room.

    The opportunity is a durable hidden SYSTEM fact used only as provenance for
    traces and generated Character messages. It is never rendered as a chat
    message and never pretends that User said something.
    """

    def __init__(self, access, repository: GroupRepository):
        self.access = access
        self.repository = repository
        self._fallback_locks_guard = threading.Lock()
        self._fallback_locks: dict[str, threading.RLock] = {}

    def _profiles(self) -> dict[str, dict]:
        return {item["id"]: item for item in self.access.character_profiles()}

    def _active_member_ids(self, group) -> list[str]:
        profiles = self._profiles()
        return [
            character_id
            for character_id in group.member_ids
            if character_id in profiles and "archived_at" not in profiles[character_id]
        ]

    def _turn_lock(self, conversation_id: str):
        scheduler = getattr(self.access, "reaction_scheduler", None)
        if scheduler is not None:
            return scheduler.group_lock_for(conversation_id)
        with self._fallback_locks_guard:
            lock = self._fallback_locks.get(conversation_id)
            if lock is None:
                lock = threading.RLock()
                self._fallback_locks[conversation_id] = lock
            return lock

    def _latest_user_id(self, conversation_id: str) -> int | None:
        event = self.repository.latest_user_event(conversation_id)
        return int(event.id) if event is not None and event.id is not None else None

    def user_quiet(self, conversation_id: str, now: datetime) -> tuple[bool, float | None]:
        latest = self.repository.latest_user_event(conversation_id)
        if latest is None:
            return True, None
        quiet_minutes = max(
            0.0,
            min(
                1440.0,
                float(getattr(self.access.settings, "group_autonomy_user_quiet_minutes", 30.0)),
            ),
        )
        age_minutes = max(0.0, (now.timestamp() - latest.event_time.timestamp()) / 60.0)
        return quiet_minutes <= 0.0 or age_minutes >= quiet_minutes, age_minutes

    def _publish_member(self, decision: dict, *, turn_id: str, watermark: int) -> None:
        hub = getattr(self.access, "stream_hub", None)
        if hub is None:
            return
        conversation_id = str(decision.get("conversation_id") or "")
        # GroupConversationService decisions intentionally stay transport-neutral,
        # so conversation_id is injected by run_opportunity before this callback.
        channel = group_channel(conversation_id)
        scheduler = getattr(self.access, "reaction_scheduler", None)
        materializer = getattr(scheduler, "voice_materializer", None) if scheduler is not None else None
        for raw_event in decision.get("emitted_events") or []:
            hub.publish(channel, "group_character_event", raw_event)
            if materializer is not None and (raw_event.get("metadata") or {}).get("action") == "VOICE_MESSAGE":
                materializer.materialize_group(raw_event)
        hub.publish(
            channel,
            "group_member_complete",
            {
                "watermark": watermark,
                "turn_id": turn_id,
                "character_id": decision.get("character_id"),
                "silent": not bool(decision.get("actions")),
                "autonomous": True,
            },
        )

    def run_opportunity(
        self,
        conversation_id: str,
        *,
        now: datetime | None = None,
        source: str = "DEV",
        respect_user_quiet: bool = False,
    ) -> dict:
        now = now or datetime.now().astimezone()
        group = self.repository.get_group(conversation_id)
        if group is None:
            raise KeyError(f"unknown or archived group: {conversation_id}")

        active_members = self._active_member_ids(group)
        if len(active_members) < 2:
            return {
                "conversation_id": conversation_id,
                "status": "SKIPPED",
                "reason": "fewer than two active character members",
                "turn_id": None,
                "message_count": 0,
                "events": [],
                "decisions": [],
            }

        quiet, age_minutes = self.user_quiet(conversation_id, now)
        if respect_user_quiet and not quiet:
            return {
                "conversation_id": conversation_id,
                "status": "SKIPPED",
                "reason": "recent user activity",
                "user_activity_age_minutes": round(float(age_minutes or 0.0), 2),
                "turn_id": None,
                "message_count": 0,
                "events": [],
                "decisions": [],
            }

        bundle = self.access.require_bundle()
        service = GroupConversationService(
            bundle.store,
            bundle.runtimes,
            bundle.clock,
            chat_service=bundle.chat,
            profiles=self.access.character_profiles(),
            turn_lock=self._turn_lock(conversation_id),
        )
        start_user_id = self._latest_user_id(conversation_id)
        turn_id = f"auto-{uuid4().hex[:12]}"
        source_event = self.repository.append_event(
            GroupEvent(
                conversation_id=conversation_id,
                turn_id=turn_id,
                actor_type="SYSTEM",
                actor_id="system",
                event_type="GROUP_OPPORTUNITY",
                event_time=now,
                content="群聊自主交流机会",
                metadata={
                    "hidden": True,
                    "autonomous": True,
                    "source": str(source or "DEV").upper(),
                    "starting_user_event_id": start_user_id,
                },
            )
        )
        watermark = int(source_event.id or 0)
        hub = getattr(self.access, "stream_hub", None)
        channel = group_channel(conversation_id)
        if hub is not None:
            hub.publish(
                channel,
                "reaction_status",
                {"state": "typing", "watermark": watermark, "turn_id": turn_id, "autonomous": True},
            )

        def current() -> bool:
            return self._latest_user_id(conversation_id) == start_user_id

        def on_member(decision: dict) -> None:
            decision["conversation_id"] = conversation_id
            self._publish_member(decision, turn_id=turn_id, watermark=watermark)

        try:
            with self._turn_lock(conversation_id):
                result = service.react_autonomous(
                    source_event,
                    active_member_ids=active_members,
                    max_messages=int(getattr(self.access.settings, "group_autonomy_max_messages", 3)),
                    commit_guard=current,
                    on_member=on_member,
                )
        except SupersededGroupReaction:
            if hub is not None:
                hub.publish(
                    channel,
                    "reaction_status",
                    {"state": "superseded", "watermark": watermark, "turn_id": turn_id, "autonomous": True},
                )
                hub.publish(
                    channel,
                    "reaction_status",
                    {"state": "idle", "watermark": watermark, "turn_id": turn_id, "autonomous": True},
                )
            return {
                "conversation_id": conversation_id,
                "status": "SUPERSEDED",
                "reason": "new user fact arrived",
                "turn_id": turn_id,
                "source_event_id": source_event.id,
                "message_count": 0,
                "events": [],
                "decisions": [],
            }

        message_count = int(result.get("message_count") or 0)
        status = "CHATTED" if message_count else "NO_CHAT"
        if hub is not None:
            hub.publish(
                channel,
                "reaction_complete",
                {
                    "watermark": watermark,
                    "turn_id": turn_id,
                    "autonomous": True,
                    "speaker_order": result.get("speaker_order") or [],
                    "message_count": message_count,
                },
            )
            hub.publish(
                channel,
                "reaction_status",
                {"state": "idle", "watermark": watermark, "turn_id": turn_id, "autonomous": True},
            )
        return {
            **result,
            "status": status,
            "source": str(source or "DEV").upper(),
            "active_member_ids": active_members,
        }


class GroupAutonomyScheduler:
    """Restart-safe process-local scheduler for bounded autonomous group chat."""

    def __init__(self, access, repository: GroupRepository, *, poll_seconds: float = 60.0):
        self.access = access
        self.repository = repository
        self.service = GroupAutonomyService(access, repository)
        self.poll_seconds = max(10.0, float(poll_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def interval_minutes(self) -> float:
        return max(
            10.0,
            min(
                10080.0,
                float(getattr(self.access.settings, "group_autonomy_interval_minutes", 360.0)),
            ),
        )

    def max_messages(self) -> int:
        return max(1, min(4, int(getattr(self.access.settings, "group_autonomy_max_messages", 3))))

    def _state_for(self, conversation_id: str, now: datetime) -> dict:
        return self.repository.ensure_autonomy_state(
            conversation_id,
            now,
            self.interval_minutes(),
        )

    def status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now().astimezone()
        profiles = self.service._profiles()
        groups = []
        for group in self.repository.list_groups():
            state = self._state_for(group.id, now)
            active_members = [
                cid for cid in group.member_ids
                if cid in profiles and "archived_at" not in profiles[cid]
            ]
            quiet, age = self.service.user_quiet(group.id, now)
            groups.append(
                {
                    "conversation_id": group.id,
                    "name": group.name,
                    "member_ids": group.member_ids,
                    "active_member_ids": active_members,
                    "last_opportunity_at": state.get("last_opportunity_at"),
                    "next_opportunity_at": state.get("next_opportunity_at"),
                    "last_status": state.get("last_status"),
                    "last_turn_id": state.get("last_turn_id"),
                    "due": epoch_us(now) >= int(state["next_opportunity_at_epoch"]),
                    "user_quiet": quiet,
                    "user_activity_age_minutes": round(float(age), 2) if age is not None else None,
                }
            )
        return {
            "enabled": autonomy_enabled(self.access),
            "interval_minutes": self.interval_minutes(),
            "max_messages": self.max_messages(),
            "user_quiet_minutes": max(
                0.0,
                min(1440.0, float(getattr(self.access.settings, "group_autonomy_user_quiet_minutes", 30.0))),
            ),
            "poll_seconds": self.poll_seconds,
            "groups": groups,
            "recent_runs": self.repository.list_autonomy_runs(limit=30),
        }

    def apply_runtime_config(
        self,
        *,
        enabled: bool | None = None,
        interval_minutes: float | None = None,
        max_messages: int | None = None,
        user_quiet_minutes: float | None = None,
        poll_seconds: float | None = None,
        rearm: bool = True,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now().astimezone()
        if enabled is not None:
            self.access.settings.group_autonomy_enabled = bool(enabled)
        if interval_minutes is not None:
            self.access.settings.group_autonomy_interval_minutes = max(10.0, min(10080.0, float(interval_minutes)))
        if max_messages is not None:
            self.access.settings.group_autonomy_max_messages = max(1, min(4, int(max_messages)))
        if user_quiet_minutes is not None:
            self.access.settings.group_autonomy_user_quiet_minutes = max(0.0, min(1440.0, float(user_quiet_minutes)))
        if poll_seconds is not None:
            self.poll_seconds = max(10.0, min(3600.0, float(poll_seconds)))
            self.access.settings.group_autonomy_poll_seconds = self.poll_seconds
        if rearm:
            next_at = now + timedelta(minutes=self.interval_minutes())
            for group in self.repository.list_groups():
                self.repository.set_next_autonomy_opportunity(group.id, next_at, now)
        self._wake.set()
        return self.status(now)

    def force_due(self, conversation_id: str, *, now: datetime | None = None) -> dict:
        now = now or datetime.now().astimezone()
        if self.repository.get_group(conversation_id) is None:
            raise KeyError(f"unknown or archived group: {conversation_id}")
        self._state_for(conversation_id, now)
        state = self.repository.set_next_autonomy_opportunity(conversation_id, now, now)
        self._wake.set()
        return state

    def run_once(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now().astimezone()
        if not autonomy_enabled(self.access):
            return []
        outcomes = []
        interval = self.interval_minutes()
        for group in self.repository.list_groups():
            state = self._state_for(group.id, now)
            if epoch_us(now) < int(state["next_opportunity_at_epoch"]):
                continue
            run_id = self.repository.claim_due_autonomy_opportunity(
                group.id,
                now,
                interval,
                source="SCHEDULED",
            )
            if run_id is None:
                continue
            try:
                result = self.service.run_opportunity(
                    group.id,
                    now=now,
                    source="SCHEDULED",
                    respect_user_quiet=True,
                )
                status = result.get("status") or "NO_CHAT"
                self.repository.finish_autonomy_run(
                    run_id,
                    group.id,
                    now,
                    status=status,
                    turn_id=result.get("turn_id"),
                    message_count=int(result.get("message_count") or 0),
                )
                outcomes.append({**result, "run_id": run_id})
            except Exception as exc:
                self.repository.finish_autonomy_run(
                    run_id,
                    group.id,
                    now,
                    status="FAILED",
                    error=str(exc),
                )
                logger.exception(
                    "group.autonomy failed conversation=%s run_id=%s error=%s",
                    group.id,
                    run_id,
                    exc,
                )
        return outcomes

    def _loop(self) -> None:
        logger.info(
            "group.autonomy scheduler_start poll_seconds=%.0f interval_minutes=%.1f",
            self.poll_seconds,
            self.interval_minutes(),
        )
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("group.autonomy scheduler_loop_error")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()
        logger.info("group.autonomy scheduler_stop")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="group-autonomy",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
