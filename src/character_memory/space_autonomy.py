from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import logging
import threading

from character_memory.domain.models import ActionType, DailyLifePlan, Event, EventType
from character_memory.space_store import SpaceRepository
from character_memory.time_utils import epoch_us


logger = logging.getLogger("character_memory.space_autonomy")

MAX_AUTONOMOUS_AUDIENCE = 10


class SpaceAutonomyService:
    """Autonomous Space behavior for the same persistent PersonRuntime.

    Daily posting uses a small DailyLifePlan decision. Seeing/commenting events
    go through PersonRuntime so Mental State, Memory, Intent and trace behavior
    remain shared with direct/group chat. Space-only actions are projected back
    into the shared SpaceRepository instead of becoming private messages.
    """

    def __init__(self, access, repository: SpaceRepository):
        self.access = access
        self.repository = repository

    def _profiles(self) -> list[dict]:
        return list(self.access.character_profiles())

    def _active_profiles(self) -> list[dict]:
        return [item for item in self._profiles() if "archived_at" not in item]

    def _require_active(self, character_id: str) -> dict:
        profile = next((item for item in self._profiles() if item["id"] == character_id), None)
        if profile is None:
            raise KeyError(f"unknown character: {character_id}")
        if "archived_at" in profile:
            raise ValueError("archived characters cannot participate in autonomous Space")
        return profile

    @staticmethod
    def _name(profile: dict) -> str:
        return str(profile.get("name") or profile.get("id") or "Character")

    def _daily_context(self, character_id: str, now: datetime, persona: str) -> str:
        mental_state = self.access.store().get_mental_state(character_id, at=now)
        memories = self.access.store().list_memories(
            character_id,
            include_inactive=False,
            limit=12,
            include_embedding=False,
        )
        recent = self.access.store().list_events(character_id, limit=24, before=now)
        memory_text = "\n".join(
            f"- [{item.memory_type}] {item.content}" for item in memories[-12:]
        ) or "- 无"
        event_text = "\n".join(
            f"- {item.event_time.isoformat()} {item.event_type.value}: {item.content}"
            for item in recent[-16:]
        ) or "- 无"
        return f"""# Persona
{persona}

# Current Mental State
{mental_state or "暂无持续心理状态。"}

# Recent Memories
{memory_text}

# Recent Events
{event_text}

# Current Time
{now.isoformat()}

# Character Space Opportunity
这是一次“要不要公开发动态”的 Opportunity，不是发帖 KPI。
系统可能在测试阶段按较短间隔再次给你 Opportunity；这不代表必须提高发帖频率。
不要为了完成任务、维持活跃、取悦用户而发动态。
只根据这个人物已经真实存在的经历、记忆和状态判断。
不要凭空创造没有发生过的新事件。

本次只判断 Space 动态：
- events 必须返回 []，不要在这里额外编造生活事件。
- social_post 可以为空；没有自然想公开表达的内容就返回 null。
- 如果发，写成这个人物自己会公开发出的自然短动态，不要写“根据我的记忆/状态”等系统口吻。
- image_prompt 暂时返回 null；图片动态会在后续独立接入。
"""

    def run_opportunity(
        self,
        character_id: str,
        *,
        now: datetime | None = None,
        cascade: bool = True,
        source: str = "DAILY",
    ) -> dict:
        now = now or datetime.now().astimezone()
        profile = self._require_active(character_id)
        bundle = self.access.require_bundle()
        runtime = bundle.runtimes.get(character_id)
        if runtime is None:
            raise KeyError(f"runtime not found for character: {character_id}")

        prompt = self._daily_context(character_id, now, runtime.persona)
        plan = bundle.model.structured_for_session(
            prompt,
            DailyLifePlan,
            f"space-opportunity:{character_id}:{now.isoformat(timespec='minutes')}:{source.lower()}",
        )
        content = str(plan.social_post or "").strip()
        if not content:
            return {
                "character_id": character_id,
                "character_name": self._name(profile),
                "posted": False,
                "post": None,
                "audience": [],
                "source": source,
            }

        source_event = self.access.store().append_event(
            Event(
                character_id=character_id,
                event_type=EventType.SOCIAL_POST,
                event_time=now,
                content=content,
                metadata={
                    "channel": "SPACE",
                    "source": source,
                },
            )
        )
        post = self.repository.create_post(
            character_id,
            content,
            now,
            source_event_id=source_event.id,
        )
        audience = self.process_audience(post.id, now=now) if cascade else []
        return {
            "character_id": character_id,
            "character_name": self._name(profile),
            "posted": True,
            "post": post.model_dump(mode="json"),
            "audience": audience,
            "source": source,
        }

    @staticmethod
    def _audience_rank(post_id: int, character_id: str) -> bytes:
        return hashlib.sha256(f"{post_id}:{character_id}".encode("utf-8")).digest()

    def select_audience(self, post_id: int, author_id: str) -> list[str]:
        candidates = [
            item["id"] for item in self._active_profiles()
            if item["id"] != author_id
        ]
        candidates.sort(key=lambda value: self._audience_rank(post_id, value))
        configured = int(getattr(self.access.settings, "space_audience_size", 5))
        size = min(
            MAX_AUTONOMOUS_AUDIENCE,
            max(0, configured),
            len(candidates),
        )
        return candidates[:size]

    def _author_reply(self, post, comment, now: datetime) -> dict | None:
        try:
            author = self._require_active(post.character_id)
        except (KeyError, ValueError):
            return None
        bundle = self.access.require_bundle()
        runtime = bundle.runtimes.get(post.character_id)
        if runtime is None:
            return None
        commenter = next(
            (item for item in self._profiles() if item["id"] == comment.character_id),
            {"id": comment.character_id, "name": comment.character_id},
        )
        result = runtime.handle(
            Event(
                character_id=post.character_id,
                event_type=EventType.SPACE_COMMENT_RECEIVED,
                event_time=now,
                content=(
                    f"{self._name(commenter)} 评论了你发布的动态“{post.content}”："
                    f"{comment.content}"
                ),
                metadata={
                    "channel": "SPACE",
                    "post_id": post.id,
                    "comment_id": comment.id,
                    "commenter_id": comment.character_id,
                    "conversation_id": f"space:{post.id}:comment:{comment.id}:author",
                },
            )
        )
        reply_action = next(
            (item for item in result.reaction.actions if item.type == ActionType.SPACE_COMMENT),
            None,
        )
        if reply_action is None or not (reply_action.message or "").strip():
            return None
        try:
            reply = self.repository.add_comment(
                post.id,
                post.character_id,
                reply_action.message,
                now,
                reply_to_comment_id=comment.id,
            )
        except ValueError:
            logger.info(
                "space.author_reply skipped post=%s author=%s reason=commenter-cap",
                post.id,
                post.character_id,
            )
            return None
        return reply.model_dump(mode="json")

    def process_audience(self, post_id: int, *, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now().astimezone()
        post = self.repository.get_post(post_id)
        if post is None:
            raise KeyError("space post not found")
        profiles = {item["id"]: item for item in self._profiles()}
        author = profiles.get(post.character_id, {"id": post.character_id, "name": post.character_id})
        bundle = self.access.require_bundle()
        outcomes: list[dict] = []

        for character_id in self.select_audience(post.id, post.character_id):
            profile = profiles.get(character_id, {"id": character_id, "name": character_id})
            runtime = bundle.runtimes.get(character_id)
            if runtime is None:
                continue
            self.repository.record_view(post.id, character_id, now)
            result = runtime.handle(
                Event(
                    character_id=character_id,
                    event_type=EventType.SPACE_POST_SEEN,
                    event_time=now,
                    content=f"{self._name(author)} 在空间发布了一条动态：{post.content}",
                    metadata={
                        "channel": "SPACE",
                        "post_id": post.id,
                        "author_id": post.character_id,
                        "conversation_id": f"space:{post.id}:viewer:{character_id}",
                    },
                )
            )

            liked = any(item.type == ActionType.SPACE_LIKE for item in result.reaction.actions)
            comment_action = next(
                (
                    item for item in result.reaction.actions
                    if item.type == ActionType.SPACE_COMMENT and (item.message or "").strip()
                ),
                None,
            )
            if liked:
                self.repository.set_reaction(post.id, character_id, "LIKE", True, now)

            comment_payload = None
            reply_payload = None
            if comment_action is not None:
                try:
                    comment = self.repository.add_comment(
                        post.id,
                        character_id,
                        comment_action.message,
                        now,
                    )
                except ValueError:
                    comment = None
                if comment is not None:
                    comment_payload = comment.model_dump(mode="json")
                    reply_payload = self._author_reply(post, comment, now)

            outcomes.append(
                {
                    "character_id": character_id,
                    "character_name": self._name(profile),
                    "viewed": True,
                    "liked": liked,
                    "comment": comment_payload,
                    "author_reply": reply_payload,
                    "actions": [
                        item.model_dump(mode="json") for item in result.reaction.actions
                    ],
                }
            )
        return outcomes


class SpaceAutonomyScheduler:
    """Restart-safe interval scheduler for autonomous Space opportunities."""

    def __init__(
        self,
        access,
        repository: SpaceRepository,
        *,
        poll_seconds: float = 60.0,
    ):
        self.access = access
        self.repository = repository
        self.service = SpaceAutonomyService(access, repository)
        self.poll_seconds = max(10.0, float(poll_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def interval_minutes(self) -> float:
        return max(
            10.0,
            min(
                10080.0,
                float(getattr(self.access.settings, "space_opportunity_interval_minutes", 1440.0)),
            ),
        )

    def _state_for(self, character_id: str, now: datetime) -> dict:
        return self.repository.ensure_opportunity_state(
            character_id,
            now,
            self.interval_minutes(),
        )

    def status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now().astimezone()
        items = []
        for profile in self.service._active_profiles():
            character_id = profile["id"]
            state = self._state_for(character_id, now)
            items.append(
                {
                    "character_id": character_id,
                    "name": profile.get("name") or character_id,
                    "last_opportunity_at": state.get("last_opportunity_at"),
                    "next_opportunity_at": state.get("next_opportunity_at"),
                    "last_status": state.get("last_status"),
                    "last_post_id": state.get("last_post_id"),
                    "due": epoch_us(now) >= int(state["next_opportunity_at_epoch"]),
                }
            )
        return {
            "enabled": autonomy_enabled(self.access),
            "interval_minutes": self.interval_minutes(),
            "poll_seconds": self.poll_seconds,
            "audience_size": min(
                MAX_AUTONOMOUS_AUDIENCE,
                max(0, int(getattr(self.access.settings, "space_audience_size", 5))),
            ),
            "characters": items,
            "recent_runs": self.repository.list_opportunity_runs(limit=30),
        }

    def apply_runtime_config(
        self,
        *,
        enabled: bool | None = None,
        interval_minutes: float | None = None,
        audience_size: int | None = None,
        poll_seconds: float | None = None,
        rearm: bool = True,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now().astimezone()
        if enabled is not None:
            self.access.settings.space_autonomy_enabled = bool(enabled)
        if interval_minutes is not None:
            self.access.settings.space_opportunity_interval_minutes = max(
                10.0, min(10080.0, float(interval_minutes))
            )
        if audience_size is not None:
            self.access.settings.space_audience_size = max(
                0, min(MAX_AUTONOMOUS_AUDIENCE, int(audience_size))
            )
        if poll_seconds is not None:
            self.poll_seconds = max(10.0, min(3600.0, float(poll_seconds)))
            self.access.settings.space_scheduler_poll_seconds = self.poll_seconds

        if rearm:
            next_at = now + timedelta(minutes=self.interval_minutes())
            for profile in self.service._active_profiles():
                self.repository.set_next_opportunity(profile["id"], next_at, now)

        self._wake.set()
        return self.status(now)

    def force_due(self, character_id: str, *, now: datetime | None = None) -> dict:
        now = now or datetime.now().astimezone()
        self.service._require_active(character_id)
        self._state_for(character_id, now)
        state = self.repository.set_next_opportunity(character_id, now, now)
        self._wake.set()
        return state

    def run_once(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now().astimezone()
        if not autonomy_enabled(self.access):
            return []
        outcomes = []
        interval = self.interval_minutes()
        for profile in self.service._active_profiles():
            character_id = profile["id"]
            state = self._state_for(character_id, now)
            if epoch_us(now) < int(state["next_opportunity_at_epoch"]):
                continue
            run_id = self.repository.claim_due_opportunity(
                character_id,
                now,
                interval,
                source="SCHEDULED",
            )
            if run_id is None:
                continue
            try:
                result = self.service.run_opportunity(
                    character_id,
                    now=now,
                    cascade=True,
                    source="SCHEDULED",
                )
                status = "POSTED" if result["posted"] else "NO_POST"
                post_id = (result.get("post") or {}).get("id")
                self.repository.finish_opportunity_run(
                    run_id,
                    character_id,
                    datetime.now().astimezone(),
                    status=status,
                    post_id=post_id,
                )
                outcomes.append({**result, "run_id": run_id})
            except Exception as exc:
                self.repository.finish_opportunity_run(
                    run_id,
                    character_id,
                    datetime.now().astimezone(),
                    status="FAILED",
                    error=str(exc),
                )
                logger.exception(
                    "space.opportunity failed character=%s run_id=%s error=%s",
                    character_id,
                    run_id,
                    exc,
                )
        return outcomes

    def _loop(self) -> None:
        logger.info(
            "space.scheduler start poll_seconds=%.0f interval_minutes=%.1f",
            self.poll_seconds,
            self.interval_minutes(),
        )
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("space.scheduler loop_error")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()
        logger.info("space.scheduler stop")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="character-space-autonomy",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)

def autonomy_enabled(access) -> bool:
    return bool(
        getattr(access.settings, "api_key", "")
        and getattr(access.settings, "space_autonomy_enabled", True)
    )
