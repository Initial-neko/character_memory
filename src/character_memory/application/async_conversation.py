from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
import threading
import time
from typing import Callable

from character_memory.application.group_conversation_service import (
    GroupConversationService,
    SupersededGroupReaction,
    effective_group_speaker_limit,
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
    # SSE Last-Event-ID survives a browser reconnect across a server restart.
    # Starting each process/channel from an epoch-based sequence prevents an old
    # high cursor from suppressing the new process's events 1, 2, 3, ... .
    next_id: int = field(default_factory=time.time_ns)
    events: deque = field(default_factory=lambda: deque(maxlen=256))
    subscribers: int = 0
    last_activity: float = field(default_factory=time.monotonic)


class ConversationEventHub:
    """Small in-process SSE event hub.

    Durable facts still live in SQLite. This hub only delivers low-latency UI
    notifications; reconnecting clients can always reconcile from history APIs.
    """

    def __init__(self, *, idle_seconds: float = 900.0):
        self._guard = threading.Lock()
        self._channels: dict[str, _HubChannel] = {}
        self._closed = threading.Event()
        self.idle_seconds = max(1.0, float(idle_seconds))

    def _prune_idle_locked(self, now: float) -> int:
        stale = [
            key
            for key, channel in self._channels.items()
            if channel.subscribers <= 0
            and now - channel.last_activity >= self.idle_seconds
        ]
        for key in stale:
            self._channels.pop(key, None)
        return len(stale)

    def prune_idle(self, *, now: float | None = None) -> int:
        """Drop inactive ephemeral SSE channels; durable facts remain in SQLite."""
        with self._guard:
            return self._prune_idle_locked(time.monotonic() if now is None else float(now))

    def _channel(self, key: str) -> _HubChannel:
        now = time.monotonic()
        with self._guard:
            self._prune_idle_locked(now)
            value = self._channels.get(key)
            if value is None:
                value = _HubChannel(last_activity=now)
                self._channels[key] = value
            else:
                value.last_activity = now
            return value

    @contextmanager
    def channel_lease(self, key: str):
        """Keep one channel alive while a stream is subscribed to it."""
        now = time.monotonic()
        with self._guard:
            self._prune_idle_locked(now)
            channel = self._channels.get(key)
            if channel is None:
                channel = _HubChannel(last_activity=now)
                self._channels[key] = channel
            channel.subscribers += 1
            channel.last_activity = now
        try:
            yield channel
        finally:
            with self._guard:
                channel.subscribers = max(0, channel.subscribers - 1)
                channel.last_activity = time.monotonic()
                # Other channels are pruned on the next activity (or explicit
                # prune_idle()). This channel has just become idle, not stale.

    def publish(self, key: str, event_type: str, data: dict) -> int:
        channel = self._channel(key)
        with channel.condition:
            seq = channel.next_id
            channel.next_id += 1
            channel.events.append((seq, event_type, data))
            channel.condition.notify_all()
            return seq

    def stream(self, key: str, *, after_id: int = 0):
        with self.channel_lease(key) as channel:
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
    image_urls: dict[int, list[str]] = field(default_factory=dict)
    mention_by_event: dict[int, list[str]] = field(default_factory=dict)
    processed_id: int = 0
    pending_since: float | None = None
    last_submit_at: float = 0.0
    active: bool = False
    generation: int = 0
    last_activity: float = field(default_factory=time.monotonic)


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
        voice_materializer=None,
        idle_seconds: float = 900.0,
    ):
        self.get_bundle = get_bundle
        self.character_profiles = character_profiles
        self.hub = hub
        self.quiet_seconds = quiet_seconds
        self.max_burst_seconds = max_burst_seconds
        self.voice_materializer = voice_materializer
        self.delivery = None
        self._workers: set[threading.Thread] = set()
        self._guard = threading.Lock()
        self._states: dict[str, _PendingState] = {}
        # Group locks stay strongly owned because a raw RLock has no explicit
        # waiter lifecycle we can safely observe. Channel/state caches below are
        # the unbounded ephemeral structures; group IDs are durable resources.
        self._group_locks: dict[str, threading.RLock] = {}
        self._closed = threading.Event()
        self.idle_seconds = max(1.0, float(idle_seconds))

    def group_lock_for(self, conversation_id: str) -> threading.RLock:
        with self._guard:
            lock = self._group_locks.get(conversation_id)
            if lock is None:
                lock = threading.RLock()
                self._group_locks[conversation_id] = lock
            return lock

    def _prune_idle_states_locked(self, now: float) -> int:
        stale = []
        for key, state in self._states.items():
            latest_id = int(state.latest_event.id) if state.latest_event is not None else state.processed_id
            if (
                not state.active
                and latest_id <= state.processed_id
                and not state.image_urls
                and not state.mention_by_event
                and now - state.last_activity >= self.idle_seconds
            ):
                stale.append(key)
        for key in stale:
            self._states.pop(key, None)
        return len(stale)

    def prune_idle(self, *, now: float | None = None) -> int:
        with self._guard:
            return self._prune_idle_states_locked(time.monotonic() if now is None else float(now))

    def _state(self, key: str) -> _PendingState:
        now = time.monotonic()
        with self._guard:
            self._prune_idle_states_locked(now)
            state = self._states.get(key)
            if state is None:
                state = _PendingState(last_activity=now)
                self._states[key] = state
            else:
                state.last_activity = now
            return state

    def status_snapshot(self, key: str) -> dict:
        """Return the current authoritative pending state for a conversation.

        SSE status events are intentionally ephemeral. A UI that closes one
        stream and later opens a brand-new stream must not keep an old local
        "typing" flag forever just because it missed the terminal idle event.
        """
        with self._guard:
            state = self._states.get(key)
            if state is not None:
                state.last_activity = time.monotonic()
        if state is None:
            return {"state": "idle", "watermark": 0}
        with state.condition:
            latest_id = int(state.latest_event.id) if state.latest_event is not None else state.processed_id
            if state.active and latest_id > state.processed_id:
                return {"state": "typing", "watermark": latest_id}
            return {"state": "idle", "watermark": state.processed_id}

    def _enqueue(
        self,
        key: str,
        event,
        image_data_urls: list[str] | None,
        target: Callable[[str, _PendingState], None],
        *,
        only_if_idle: bool = False,
    ) -> bool:
        if event.id is None:
            raise ValueError("asynchronous reaction requires a persisted event id")
        state = self._state(key)
        now = time.monotonic()
        should_start = False
        queued_watermark = 0
        worker_generation = 0
        incoming_id = int(event.id)
        urls = [str(value).strip() for value in (image_data_urls or []) if str(value).strip()]
        with state.condition:
            if only_if_idle:
                latest_id = int(state.latest_event.id) if state.latest_event is not None else state.processed_id
                if state.active or latest_id > state.processed_id:
                    logger.debug("scheduler.enqueue skipped_busy channel=%s incoming=%s", key, incoming_id)
                    return False
            # Persistence order is authoritative. Request threads may reach this
            # method out of order, so the in-memory watermark must never move
            # backwards after a newer durable event has already been observed.
            if incoming_id <= state.processed_id:
                logger.info(
                    "scheduler.enqueue ignored_processed channel=%s incoming=%s processed=%s",
                    key,
                    incoming_id,
                    state.processed_id,
                )
                return False

            current_id = int(state.latest_event.id) if state.latest_event is not None else state.processed_id
            if incoming_id > current_id:
                state.latest_event = event
            else:
                logger.info(
                    "scheduler.enqueue kept_newer channel=%s incoming=%s current=%s",
                    key,
                    incoming_id,
                    current_id,
                )

            if urls:
                state.image_urls[incoming_id] = list(dict.fromkeys(urls))
            metadata = getattr(event, "metadata", {}) or {}
            raw_mentions = metadata.get("mentions") if isinstance(metadata, dict) else None
            if isinstance(raw_mentions, list) and raw_mentions:
                state.mention_by_event[incoming_id] = [str(value) for value in raw_mentions if str(value).strip()]
            if state.pending_since is None:
                state.pending_since = now
            state.last_submit_at = now
            state.last_activity = now
            queued_watermark = int(state.latest_event.id) if state.latest_event is not None else incoming_id
            if not state.active and queued_watermark > state.processed_id:
                state.active = True
                state.generation += 1
                worker_generation = state.generation
                should_start = True
            state.condition.notify_all()
        self.hub.publish(key, "reaction_status", {"state": "queued", "watermark": queued_watermark})
        if should_start:
            def run():
                try:
                    target(key, state)
                finally:
                    with self._guard:
                        self._workers.discard(threading.current_thread())
                    with state.condition:
                        if state.generation == worker_generation:
                            state.active = False
                            state.condition.notify_all()
            worker = threading.Thread(target=run, daemon=True, name=f"reaction-{key[:32]}")
            with self._guard:
                if self._closed.is_set():
                    with state.condition:
                        state.active = False
                    return False
                self._workers.add(worker)
                worker.start()
        return True

    @staticmethod
    def _image_urls(image_data_url: str | None = None, image_data_urls: list[str] | None = None) -> list[str]:
        values = [str(value).strip() for value in (image_data_urls or []) if str(value).strip()]
        if image_data_url and str(image_data_url).strip():
            values.append(str(image_data_url).strip())
        return list(dict.fromkeys(values))

    def enqueue_direct(
        self,
        character_id: str,
        conversation_id: str,
        event,
        *,
        image_data_url: str | None = None,
        image_data_urls: list[str] | None = None,
        only_if_idle: bool = False,
    ) -> bool:
        key = direct_channel(character_id, conversation_id)
        event.metadata.setdefault("conversation_id", conversation_id)
        return self._enqueue(
            key,
            event,
            self._image_urls(image_data_url, image_data_urls),
            lambda channel, state: self._run_direct(channel, state, character_id, conversation_id),
            only_if_idle=only_if_idle,
        )

    def enqueue_group(
        self,
        conversation_id: str,
        event,
        *,
        image_data_url: str | None = None,
        image_data_urls: list[str] | None = None,
        only_if_idle: bool = False,
    ) -> None:
        key = group_channel(conversation_id)
        self._enqueue(
            key,
            event,
            self._image_urls(image_data_url, image_data_urls),
            lambda channel, state: self._run_group(channel, state, conversation_id),
            only_if_idle=only_if_idle,
        )

    @staticmethod
    def _ordered_mentions(state: _PendingState, watermark: int) -> list[str]:
        result: list[str] = []
        for event_id in sorted(state.mention_by_event):
            if not (state.processed_id < event_id <= watermark):
                continue
            for value in state.mention_by_event[event_id]:
                if value == "*":
                    return ["*"]
                if value not in result:
                    result.append(value)
        return result

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
                image_urls = [
                    url
                    for event_id, urls in sorted(state.image_urls.items())
                    if state.processed_id < event_id <= watermark
                    for url in urls
                ]
                mentions = self._ordered_mentions(state, watermark)
                state.pending_since = None
                return event, watermark, image_urls, mentions
        return None

    @staticmethod
    def _latest_direct_user_id(store, character_id: str, conversation_id: str) -> int | None:
        # Supersession follows durable arrival order, not caller-controlled event
        # timestamps. A later persisted message with an older `at` value must
        # still supersede an in-flight generation for an earlier Event.
        return store.latest_direct_event_id(
            character_id,
            conversation_id,
            event_types=("USER_MESSAGE",),
        )

    @staticmethod
    def _latest_direct_visual_source_id(store, character_id: str, conversation_id: str) -> int | None:
        """Newest direct fact that can supersede a periodic visual observation.

        A later user message always wins. A later visual observation also makes
        the older screen reaction stale. Ordinary character output does neither.
        """
        newest = store.fact_reader.newest_direct_input(
            character_id, conversation_id, visual=True,
        )
        return newest[1] if newest is not None else None

    @staticmethod
    def _input_identity(store, character_id: str, conversation_id: str, event_id: int):
        return store.fact_reader.direct_input_identity(character_id, conversation_id, event_id)

    @staticmethod
    def _direct_response_events(store, character_id: str, source_event_id: int):
        return store.list_character_response_events(character_id, source_event_id)

    def publish_direct_responses(
        self,
        store,
        character_id: str,
        conversation_id: str,
        source_event_id: int,
    ) -> list[dict]:
        """Publish one direct reaction and materialize any voice messages.

        User-message reactions, proactive intents and TIME_TICK wakes all persist
        the same CHARACTER_MESSAGE facts. Keeping transport/materialization here
        prevents background producers from publishing text while leaving a
        VOICE_MESSAGE permanently pending.
        """
        channel = direct_channel(character_id, conversation_id)
        published: list[dict] = []
        for response_event in self._direct_response_events(store, character_id, source_event_id):
            raw_event = {
                "id": response_event.id,
                "character_id": response_event.character_id,
                "event_type": response_event.event_type.value,
                "event_time": response_event.event_time.isoformat(),
                "content": response_event.content,
                "metadata": response_event.metadata,
            }
            self.hub.publish(channel, "character_event", raw_event)
            if self.voice_materializer is not None and response_event.metadata.get("action") == "VOICE_MESSAGE":
                self.voice_materializer.materialize_direct(response_event, conversation_id=conversation_id)
            published.append(raw_event)
        invitations=[]
        with store._lock:
            if not self._closed.is_set() and store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_invitations'").fetchone():
                invitations=store.conn.execute("SELECT id FROM call_invitations WHERE character_id=? AND conversation_id=? AND source_event_id=?",[character_id,conversation_id,source_event_id]).fetchall()
        for invitation in invitations:
            self.hub.publish(channel,"call_invitation",{"id":invitation['id']})
        return published

    def _finish_cycle(self, state: _PendingState, watermark: int) -> bool:
        with state.condition:
            state.processed_id = max(state.processed_id, watermark)
            for event_id in [value for value in state.image_urls if value <= watermark]:
                state.image_urls.pop(event_id, None)
            for event_id in [value for value in state.mention_by_event if value <= watermark]:
                state.mention_by_event.pop(event_id, None)
            latest_id = int(state.latest_event.id) if state.latest_event is not None else state.processed_id
            if latest_id <= state.processed_id:
                state.active = False
                state.last_activity = time.monotonic()
                return True
            if state.pending_since is None:
                state.pending_since = time.monotonic()
            return False

    def _retry_superseded(self, state: _PendingState) -> None:
        """Retry from the newest fact without consuming the stale watermark.

        Image bytes and mention signals from a superseded generation stay
        available to the next context snapshot so a follow-up text does not lose
        the visual or directed-attention part of the same user burst.
        """
        with state.condition:
            if state.pending_since is None:
                state.pending_since = time.monotonic()
            state.condition.notify_all()

    def prepare_recovery(self, channel, event_id):
        state = self._state(channel)
        with state.condition:
            if state.active:
                return False
            state.processed_id = min(state.processed_id, event_id - 1)
            state.latest_event = None
            return True

    def _run_direct(self, channel: str, state: _PendingState, character_id: str, conversation_id: str) -> None:
        while not self._closed.is_set():
            snapshot = self._snapshot_after_quiet(state)
            if snapshot is None:
                return
            event, watermark, image_urls, _mentions = snapshot
            self.hub.publish(channel, "reaction_status", {"state": "typing", "watermark": watermark})
            superseded = False
            batch = None
            cycle_error = None
            try:
                bundle = self.get_bundle()
                if self.delivery is not None:
                    batch, persisted_urls = self.delivery.begin(event, kind="EVENT", bundle=bundle)
                    if batch is not None:
                        image_urls = persisted_urls
                runtime = bundle.runtimes[character_id]
                def current():
                    durable = self.delivery is None or self.delivery.guard(batch, character_id)
                    identity = self._input_identity(bundle.store, character_id, conversation_id, event.id)
                    newest = (bundle.store.fact_reader.newest_direct_input(
                        character_id, conversation_id, visual=True)
                        if event.event_type.value == "VISUAL_OBSERVATION"
                        else bundle.store.fact_reader.newest_direct_input(
                            character_id, conversation_id, visual=False))
                    # Compare (event_time, id, space): chat and non-chat inputs
                    # share neither table nor id space, and arrival order alone
                    # cannot order them.
                    return durable and not self._closed.is_set() and identity is not None and newest is not None and newest <= identity
                with bundle.chat._lock_for(character_id):
                    bundle.store.set_world_time(character_id, event.event_time)
                    result = runtime.handle(
                        event,
                        image_data_urls=image_urls or None,
                        persist_event=False,
                        commit_guard=current,
                    )
                if self.delivery is not None:
                    self.delivery.finish(batch)
                if not result.replayed:
                    self.publish_direct_responses(
                        bundle.store,
                        character_id,
                        conversation_id,
                        watermark,
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
                cycle_error = exc
                logger.exception("scheduler.direct failed character=%s conversation=%s", character_id, conversation_id)
                self.hub.publish(channel, "reaction_error", {"watermark": watermark, "message": str(exc)})

            if self.delivery is not None and batch is not None:
                self.delivery.finish(batch, superseded=superseded, error=cycle_error)
            if superseded:
                self._retry_superseded(state)
                continue
            idle = self._finish_cycle(state, watermark)
            if idle:
                self.hub.publish(channel, "reaction_status", {"state": "idle", "watermark": watermark})
                return

    @staticmethod
    def _latest_group_user_id(service: GroupConversationService, conversation_id: str) -> int | None:
        return service.latest_user_insertion_id(conversation_id)

    def _run_group(self, channel: str, state: _PendingState, conversation_id: str) -> None:
        while not self._closed.is_set():
            snapshot = self._snapshot_after_quiet(state)
            if snapshot is None:
                return
            event, watermark, image_urls, mentions = snapshot
            self.hub.publish(channel, "reaction_status", {"state": "typing", "watermark": watermark})
            superseded = False
            batch = None
            cycle_error = None
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
                # Backend shape knob, read once per turn from the bundle's
                # settings snapshot, so it is restart_required like the rest of
                # the config surface (HOT_APPLY_FIELDS only covers TTS).
                # Mentioned members always participate regardless.
                max_speakers = effective_group_speaker_limit(bundle.settings)
                if self.delivery is not None:
                    batch, persisted_urls = self.delivery.begin(event, kind="CONVERSATION_EVENT", bundle=bundle,
                        service=service, max_speakers=max_speakers)
                    if batch is not None:
                        image_urls = persisted_urls
                        mentions = batch.snapshot["mentions"]

                def current() -> bool:
                    return ((self.delivery is None or self.delivery.guard(batch)) and not self._closed.is_set()
                        and self._latest_group_user_id(service, conversation_id) == watermark)

                def member_done(decision: dict) -> None:
                    if decision.get("replayed"):
                        return
                    for raw_event in decision.get("emitted_events") or []:
                        self.hub.publish(channel, "group_character_event", raw_event)
                        if self.voice_materializer is not None and (raw_event.get("metadata") or {}).get("action") == "VOICE_MESSAGE":
                            self.voice_materializer.materialize_group(raw_event)
                    self.hub.publish(
                        channel,
                        "group_member_complete",
                        {
                            "watermark": watermark,
                            "turn_id": event.turn_id,
                            "character_id": decision.get("character_id"),
                            "silent": not bool(decision.get("actions")),
                            "explicitly_mentioned": bool(decision.get("explicitly_mentioned")),
                        },
                    )

                with self.group_lock_for(conversation_id):
                    result = service.react_from_event(
                        event,
                        image_data_urls=image_urls or None,
                        commit_guard=current,
                        on_member=member_done,
                        mention_order=mentions,
                        max_speakers=max_speakers,
                        frozen_plan=batch.snapshot["plan"] if batch is not None else None,
                        member_guard=(lambda person: self.delivery.guard(batch, person, conversation_id=conversation_id))
                            if batch is not None else None,
                    )
                if any(decision.get("error") for decision in result.get("decisions", [])):
                    cycle_error = RuntimeError("group member did not complete")
                if self.delivery is not None:
                    self.delivery.finish(batch, error=cycle_error)
                self.hub.publish(
                    channel,
                    "reaction_complete",
                    {
                        "watermark": watermark,
                        "turn_id": event.turn_id,
                        "mentions": mentions,
                        "speaker_order": result.get("speaker_order") or [],
                        "deferred_speaker_ids": result.get("deferred_speaker_ids") or [],
                        "max_speakers": result.get("max_speakers"),
                        "decisions": [
                            {
                                "character_id": item.get("character_id"),
                                "silent": not bool(item.get("actions")),
                                "explicitly_mentioned": bool(item.get("explicitly_mentioned")),
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
                cycle_error = exc
                logger.exception("scheduler.group failed conversation=%s", conversation_id)
                self.hub.publish(channel, "reaction_error", {"watermark": watermark, "turn_id": event.turn_id, "message": str(exc)})

            if self.delivery is not None and batch is not None:
                self.delivery.finish(batch, superseded=superseded, error=cycle_error)
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
        # Providers own bounded request timeouts. Keep the shared SQLite store
        # alive until all model callbacks have observed shutdown and exited.
        with self._guard:
            workers = list(self._workers)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join()
