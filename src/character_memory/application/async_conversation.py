from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import logging
import threading
import time
from typing import Callable

from character_memory.application.group_conversation_service import (
    GroupConversationService,
    SupersededGroupReaction,
)
from character_memory.runtime.person_runtime import SupersededReaction


logger = logging.getLogger("character_memory.application.async_conversation")


def direct_channel(character_id: str, conversation_id: str) -> str:
    return f"direct:{character_id}:{conversation_id}"


def group_channel(conversation_id: str) -> str:
    return f"group:{conversation_id}"


@dataclass
class _HubChannel:
    condition: threading.Condition = field(default_factory=threading.Condition)
    next_id: int = 1
    events: deque = field(default_factory=lambda: deque(maxlen=256))


class ConversationEventHub:
    """Small in-process SSE event hub.

    Durable facts still live in SQLite. This hub only delivers low-latency UI
    notifications; reconnecting clients can always reconcile from history APIs.
    """

    def __init__(self):
        self._guard = threading.Lock()
        self._channels: dict[str, _HubChannel] = {}
        self._closed = threading.Event()

    def _channel(self, key: str) -> _HubChannel:
        with self._guard:
            value = self._channels.get(key)
            if value is None:
                value = _HubChannel()
                self._channels[key] = value
            return value

    def publish(self, key: str, event_type: str, data: dict) -> int:
        channel = self._channel(key)
        with channel.condition:
            seq = channel.next_id
            channel.next_id += 1
            channel.events.append((seq, event_type, data))
            channel.condition.notify_all()
            return seq

    def stream(self, key: str, *, after_id: int = 0):
        channel = self._channel(key)
        cursor = max(0, int(after_id or 0))
        yield "retry: 1500\n\n"
        while not self._closed.is_set():
            batch = []
            with channel.condition:
                batch = [item for item in channel.events if item[0] > cursor]
                if not batch:
                    channel.condition.wait(timeout=15.0)
                    batch = [item for item in channel.events if item[0] > cursor]
            if not batch:
                yield ": ping\n\n"
                continue
            for seq, event_type, data in batch:
                cursor = seq
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {seq}\nevent: {event_type}\ndata: {payload}\n\n"

    def close(self) -> None:
        self._closed.set()
        with self._guard:
            channels = list(self._channels.values())
        for channel in channels:
            with channel.condition:
                channel.condition.notify_all()


@dataclass
class _PendingState:
    condition: threading.Condition = field(default_factory=threading.Condition)
    latest_event: object | None = None
    image_urls: dict[int, str] = field(default_factory=dict)
    processed_id: int = 0
    pending_since: float | None = None
    last_submit_at: float = 0.0
    active: bool = False


class ReactionScheduler:
    """Per-conversation coalescing reaction scheduler.

    User facts are persisted before enqueue. Workers wait for a short quiet
    window, react to the latest fact, and use a commit watermark so generations
    made stale by newer user facts cannot mutate derived state.
    """

    def __init__(
        self,
        get_bundle: Callable,
        character_profiles: Callable[[], list[dict]],
        hub: ConversationEventHub,
        *,
        quiet_seconds: float = 0.5,
        max_burst_seconds: float = 1.5,
    ):
        self.get_bundle = get_bundle
        self.character_profiles = character_profiles
        self.hub = hub
        self.quiet_seconds = quiet_seconds
        self.max_burst_seconds = max_burst_seconds
        self._guard = threading.Lock()
        self._states: dict[str, _PendingState] = {}
        self._group_locks: dict[str, threading.RLock] = {}
        self._closed = threading.Event()

    def group_lock_for(self, conversation_id: str) -> threading.RLock:
        with self._guard:
            lock = self._group_locks.get(conversation_id)
            if lock is None:
                lock = threading.RLock()
                self._group_locks[conversation_id] = lock
            return lock

    def _state(self, key: str) -> _PendingState:
        with self._guard:
            state = self._states.get(key)
            if state is None:
                state = _PendingState()
                self._states[key] = state
            return state

    def _enqueue(self, key: str, event, image_data_url: str | None, target: Callable[[str, _PendingState], None]) -> None:
        if event.id is None:
            raise ValueError("asynchronous reaction requires a persisted event id")
        state = self._state(key)
        now = time.monotonic()
        should_start = False
        with state.condition:
            state.latest_event = event
            if image_data_url:
                state.image_urls[int(event.id)] = image_data_url
            if state.pending_since is None:
                state.pending_since = now
            state.last_submit_at = now
            if not state.active:
                state.active = True
                should_start = True
            state.condition.notify_all()
        self.hub.publish(key, "reaction_status", {"state": "queued", "watermark": int(event.id)})
        if should_start:
            threading.Thread(target=target, args=(key, state), daemon=True, name=f"reaction-{key[:32]}").start()

    def enqueue_direct(self, character_id: str, conversation_id: str, event, *, image_data_url: str | None = None) -> None:
        key = direct_channel(character_id, conversation_id)
        event.metadata.setdefault("conversation_id", conversation_id)
        self._enqueue(
            key,
            event,
            image_data_url,
            lambda channel, state: self._run_direct(channel, state, character_id, conversation_id),
        )

    def enqueue_group(self, conversation_id: str, event, *, image_data_url: str | None = None) -> None:
        key = group_channel(conversation_id)
        self._enqueue(
            key,
            event,
            image_data_url,
            lambda channel, state: self._run_group(channel, state, conversation_id),
        )

    def _snapshot_after_quiet(self, state: _PendingState):
        while not self._closed.is_set():
            with state.condition:
                event = state.latest_event
                if event is None:
                    state.active = False
                    return None
                pending_since = state.pending_since or time.monotonic()
                target = min(
                    state.last_submit_at + self.quiet_seconds,
                    pending_since + self.max_burst_seconds,
                )
                remaining = target - time.monotonic()
                if remaining > 0:
                    state.condition.wait(timeout=remaining)
                    continue
                watermark = int(event.id)
                image_urls = [url for event_id, url in sorted(state.image_urls.items()) if state.processed_id < event_id <= watermark]
                state.pending_since = None
                return event, watermark, image_urls
        return None

    @staticmethod
    def _latest_direct_user_id(store, character_id: str, conversation_id: str) -> int | None:
        with store._lock:
            row = store.conn.execute(
                "SELECT id FROM events WHERE character_id=? AND event_type='USER_MESSAGE' AND event_time_epoch IS NOT NULL "
                "AND json_extract(metadata_json,'$.conversation_id')=? ORDER BY event_time_epoch DESC,id DESC LIMIT 1",
                (character_id, conversation_id),
            ).fetchone()
        return int(row["id"]) if row else None

    @staticmethod
    def _direct_response_events(store, character_id: str, source_event_id: int):
        with store._lock:
            rows = store.conn.execute(
                "SELECT * FROM events WHERE character_id=? AND event_type='CHARACTER_MESSAGE' "
                "AND CAST(json_extract(metadata_json,'$.source_event_id') AS INTEGER)=? ORDER BY id",
                (character_id, int(source_event_id)),
            ).fetchall()
        return [store._event_from_row(row) for row in rows]

    def _finish_cycle(self, state: _PendingState, watermark: int) -> bool:
        with state.condition:
            state.processed_id = max(state.processed_id, watermark)
            for event_id in [value for value in state.image_urls if value <= watermark]:
                state.image_urls.pop(event_id, None)
            latest_id = int(state.latest_event.id) if state.latest_event is not None else state.processed_id
            if latest_id <= state.processed_id:
                state.active = False
                return True
            if state.pending_since is None:
                state.pending_since = time.monotonic()
            return False

    def _retry_superseded(self, state: _PendingState) -> None:
        """Retry from the newest fact without consuming the stale watermark.

        In particular, image bytes from a superseded generation stay available
        to the next context snapshot; otherwise a follow-up text could retain an
        image placeholder while losing the actual visual input.
        """
        with state.condition:
            if state.pending_since is None:
                state.pending_since = time.monotonic()
            state.condition.notify_all()

    def _run_direct(self, channel: str, state: _PendingState, character_id: str, conversation_id: str) -> None:
        while not self._closed.is_set():
            snapshot = self._snapshot_after_quiet(state)
            if snapshot is None:
                return
            event, watermark, image_urls = snapshot
            self.hub.publish(channel, "reaction_status", {"state": "typing", "watermark": watermark})
            superseded = False
            try:
                bundle = self.get_bundle()
                runtime = bundle.runtimes[character_id]
                with bundle.chat._lock_for(character_id):
                    bundle.store.set_world_time(character_id, event.event_time)
                    result = runtime.handle(
                        event,
                        image_data_urls=image_urls or None,
                        persist_event=False,
                        commit_guard=lambda: self._latest_direct_user_id(bundle.store, character_id, conversation_id) == watermark,
                    )
                for response_event in self._direct_response_events(bundle.store, character_id, watermark):
                    self.hub.publish(
                        channel,
                        "character_event",
                        {
                            "id": response_event.id,
                            "character_id": response_event.character_id,
                            "event_type": response_event.event_type.value,
                            "event_time": response_event.event_time.isoformat(),
                            "content": response_event.content,
                            "metadata": response_event.metadata,
                        },
                    )
                self.hub.publish(
                    channel,
                    "reaction_complete",
                    {
                        "watermark": watermark,
                        "silent": not bool(result.reaction.actions),
                        "perception": result.reaction.perception,
                        "reaction": result.reaction.reaction,
                    },
                )
            except SupersededReaction:
                superseded = True
                logger.info("scheduler.direct superseded character=%s conversation=%s watermark=%s", character_id, conversation_id, watermark)
                self.hub.publish(channel, "reaction_status", {"state": "superseded", "watermark": watermark})
            except Exception as exc:
                logger.exception("scheduler.direct failed character=%s conversation=%s", character_id, conversation_id)
                self.hub.publish(channel, "reaction_error", {"watermark": watermark, "message": str(exc)})

            if superseded:
                self._retry_superseded(state)
                continue
            idle = self._finish_cycle(state, watermark)
            if idle:
                self.hub.publish(channel, "reaction_status", {"state": "idle", "watermark": watermark})
                return

    def _latest_group_user_id(self, service: GroupConversationService, conversation_id: str) -> int | None:
        return service._latest_user_event_id(conversation_id)

    def _run_group(self, channel: str, state: _PendingState, conversation_id: str) -> None:
        while not self._closed.is_set():
            snapshot = self._snapshot_after_quiet(state)
            if snapshot is None:
                return
            event, watermark, image_urls = snapshot
            self.hub.publish(channel, "reaction_status", {"state": "typing", "watermark": watermark})
            superseded = False
            try:
                bundle = self.get_bundle()
                service = GroupConversationService(
                    bundle.store,
                    bundle.runtimes,
                    bundle.clock,
                    chat_service=bundle.chat,
                    profiles=self.character_profiles(),
                    turn_lock=self.group_lock_for(conversation_id),
                )

                def current() -> bool:
                    return self._latest_group_user_id(service, conversation_id) == watermark

                def member_done(decision: dict) -> None:
                    for raw_event in decision.get("emitted_events") or []:
                        self.hub.publish(channel, "group_character_event", raw_event)
                    self.hub.publish(
                        channel,
                        "group_member_complete",
                        {
                            "watermark": watermark,
                            "turn_id": event.turn_id,
                            "character_id": decision.get("character_id"),
                            "silent": not bool(decision.get("actions")),
                        },
                    )

                with self.group_lock_for(conversation_id):
                    result = service.react_from_event(
                        event,
                        image_data_urls=image_urls or None,
                        commit_guard=current,
                        on_member=member_done,
                    )
                self.hub.publish(
                    channel,
                    "reaction_complete",
                    {
                        "watermark": watermark,
                        "turn_id": event.turn_id,
                        "speaker_order": result.get("speaker_order") or [],
                        "decisions": [
                            {
                                "character_id": item.get("character_id"),
                                "silent": not bool(item.get("actions")),
                            }
                            for item in result.get("decisions") or []
                        ],
                    },
                )
            except SupersededGroupReaction:
                superseded = True
                logger.info("scheduler.group superseded conversation=%s watermark=%s", conversation_id, watermark)
                self.hub.publish(channel, "reaction_status", {"state": "superseded", "watermark": watermark, "turn_id": event.turn_id})
            except Exception as exc:
                logger.exception("scheduler.group failed conversation=%s", conversation_id)
                self.hub.publish(channel, "reaction_error", {"watermark": watermark, "turn_id": event.turn_id, "message": str(exc)})

            if superseded:
                self._retry_superseded(state)
                continue
            idle = self._finish_cycle(state, watermark)
            if idle:
                self.hub.publish(channel, "reaction_status", {"state": "idle", "watermark": watermark})
                return

    def close(self) -> None:
        self._closed.set()
        with self._guard:
            states = list(self._states.values())
        for state in states:
            with state.condition:
                state.condition.notify_all()
