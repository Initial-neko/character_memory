from __future__ import annotations

import base64
from collections import deque
import hashlib
import logging
import re
import threading
import time
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from character_memory.live2d_behavior import Live2DPresentationRequest, bind_presentation

from character_memory.application.async_conversation import direct_channel
from character_memory.application.chat_service import build_user_event
from character_memory.application.group_conversation_service import build_group_user_event, resolve_group_mentions
from character_memory.domain.models import Event, EventType
from character_memory.group_store import GroupRepository
from character_memory.media import MediaStorage


logger = logging.getLogger("character_memory.visual_capture_web")
_VISUAL_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.S)
_MAX_FRAME_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 6 * 1024 * 1024


class VisualFrameRequest(BaseModel):
    filename: str = Field(default="visual-frame.jpg", min_length=1, max_length=180)
    data_url: str = Field(min_length=16)
    source: Literal["CAMERA", "DISPLAY"]
    captured_at_ms: int | None = Field(default=None, ge=0)


class DirectVisualMessageRequest(BaseModel):
    live2d: Live2DPresentationRequest | None = None
    message: str = Field(min_length=1, max_length=12000)
    visual_frames: list[VisualFrameRequest] = Field(min_length=1, max_length=5)
    character_id: str = "rin"
    conversation_id: str = "default"
    at: datetime | None = None


class GroupVisualMessageRequest(BaseModel):
    live2d: Live2DPresentationRequest | None = None
    message: str = Field(min_length=1, max_length=12000)
    visual_frames: list[VisualFrameRequest] = Field(min_length=1, max_length=5)
    mentions: list[str] = Field(default_factory=list, max_length=4)
    at: datetime | None = None


class DirectVisualObservationRequest(BaseModel):
    character_id: str = "rin"
    conversation_id: str = "default"
    visual_frame: VisualFrameRequest


class PeriodicVisualObservationGate:
    """Process-local cost/dedup gate in front of paid periodic Vision turns."""

    def __init__(self):
        self._lock = threading.Lock()
        self._accepted: dict[str, deque[float]] = {}
        self._last_at: dict[str, float] = {}
        self._last_digest: dict[str, str] = {}

    def claim(
        self,
        key: str,
        frame_digest: str,
        *,
        interval_seconds: float,
        max_per_hour: int,
        now: float | None = None,
    ) -> tuple[bool, str]:
        stamp = time.monotonic() if now is None else float(now)
        interval = max(0.0, float(interval_seconds))
        hourly = max(0, int(max_per_hour))
        if hourly <= 0:
            return False, "HOURLY_LIMIT_DISABLED"
        with self._lock:
            recent = self._accepted.setdefault(key, deque())
            floor = stamp - 3600.0
            while recent and recent[0] <= floor:
                recent.popleft()
            if self._last_digest.get(key) == frame_digest:
                return False, "DUPLICATE_FRAME"
            previous = self._last_at.get(key)
            if previous is not None and stamp - previous < interval:
                return False, "INTERVAL"
            if len(recent) >= hourly:
                return False, "HOURLY_LIMIT"
            recent.append(stamp)
            self._last_at[key] = stamp
            self._last_digest[key] = frame_digest
            return True, "ACCEPTED"


_PERIODIC_VISUAL_GATE = PeriodicVisualObservationGate()


def normalize_visual_frames(frames: list[VisualFrameRequest]) -> tuple[list[str], dict]:
    """Validate transient browser frames without persisting their bytes.

    Camera/screen frames are observation context for one model turn, not chat
    attachments. Only a small metadata summary is allowed into durable events.
    """
    normalized: list[str] = []
    total_bytes = 0
    sources: list[str] = []
    captured: list[int] = []
    for frame in frames:
        match = _VISUAL_DATA_URL_RE.match(frame.data_url.strip())
        if match is None:
            raise ValueError("visual frame must be a base64 image data URL")
        try:
            payload = base64.b64decode(match.group(2), validate=True)
        except ValueError as exc:
            raise ValueError("visual frame base64 is invalid") from exc
        if not payload:
            raise ValueError("visual frame is empty")
        if len(payload) > _MAX_FRAME_BYTES:
            raise ValueError("visual frame exceeds 2 MiB limit")
        total_bytes += len(payload)
        if total_bytes > _MAX_TOTAL_BYTES:
            raise ValueError("visual frames exceed 6 MiB total limit")
        mime_type = MediaStorage._sniff_mime(payload)
        if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("visual frames must be JPEG, PNG or WebP")
        normalized.append(f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}")
        if frame.source not in sources:
            sources.append(frame.source)
        if frame.captured_at_ms is not None:
            captured.append(frame.captured_at_ms)

    metadata = {
        "frame_count": len(normalized),
        "sources": sources,
    }
    if captured:
        metadata["captured_at_ms"] = {"first": min(captured), "last": max(captured)}
    return normalized, metadata


def attach_visual_capture_routes(app):
    """Attach camera/screen keyframe routes after the async scheduler exists."""
    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before visual capture routes are attached")

    def profiles_by_id() -> dict[str, dict]:
        return {item["id"]: item for item in access.character_profiles()}

    def ensure_character(character_id: str) -> None:
        if character_id not in profiles_by_id():
            raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")

    def scheduler():
        value = getattr(access, "reaction_scheduler", None)
        if value is None:
            raise HTTPException(status_code=503, detail="async reaction scheduler is not ready")
        return value

    @app.get("/v1/visual/periodic/config")
    def periodic_visual_config():
        return {
            "enabled": bool(getattr(access.settings, "periodic_visual_observation_enabled", True)),
            "interval_seconds": float(
                getattr(access.settings, "periodic_visual_observation_interval_seconds", 30.0)
            ),
            "max_per_hour": int(
                getattr(access.settings, "periodic_visual_observation_max_per_hour", 6)
            ),
            "scope": "DIRECT_DISPLAY_ONLY",
        }

    @app.post("/v1/visual/direct/observations", status_code=202)
    def accept_direct_visual_observation(req: DirectVisualObservationRequest):
        """Accept one low-priority screen-change observation.

        The browser already performs cheap pixel-difference filtering. This
        endpoint adds server-side cost/dedup/busy guards before a Vision call.
        Raw frame bytes are only handed to the reaction worker and never stored.
        """
        ensure_character(req.character_id)
        if req.visual_frame.source != "DISPLAY":
            raise HTTPException(status_code=400, detail="periodic visual observation only accepts DISPLAY frames")
        if not bool(getattr(access.settings, "periodic_visual_observation_enabled", True)):
            return {"accepted": False, "reason": "DISABLED"}

        reaction_scheduler = scheduler()
        key = direct_channel(req.character_id, req.conversation_id)
        if reaction_scheduler.status_snapshot(key).get("state") != "idle":
            return {"accepted": False, "reason": "BUSY"}

        try:
            frame_urls, visual_metadata = normalize_visual_frames([req.visual_frame])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        digest = hashlib.sha256(frame_urls[0].encode("utf-8")).hexdigest()
        claimed, reason = _PERIODIC_VISUAL_GATE.claim(
            key,
            digest,
            interval_seconds=float(
                getattr(access.settings, "periodic_visual_observation_interval_seconds", 30.0)
            ),
            max_per_hour=int(
                getattr(access.settings, "periodic_visual_observation_max_per_hour", 6)
            ),
        )
        if not claimed:
            return {"accepted": False, "reason": reason}

        now = datetime.now().astimezone()
        event = access.store().append_event(
            Event(
                character_id=req.character_id,
                event_type=EventType.VISUAL_OBSERVATION,
                event_time=now,
                content="屏幕共享画面出现了新的显著变化；请结合本轮临时图像判断是否值得自然回应。",
                metadata={
                    "channel": "DIRECT",
                    "conversation_id": req.conversation_id,
                    "periodic_screen_observation": True,
                    "visual_capture": visual_metadata,
                },
            )
        )
        queued = reaction_scheduler.enqueue_direct(
            req.character_id,
            req.conversation_id,
            event,
            image_data_urls=frame_urls,
            only_if_idle=True,
        )
        if not queued:
            logger.info(
                "visual.periodic skipped_busy_race character=%s conversation=%s event_id=%s",
                req.character_id,
                req.conversation_id,
                event.id,
            )
            return {"accepted": False, "reason": "BUSY_RACE", "event_id": event.id}

        logger.info(
            "visual.periodic accepted character=%s conversation=%s event_id=%s",
            req.character_id,
            req.conversation_id,
            event.id,
        )
        return {
            "accepted": True,
            "reason": "ACCEPTED",
            "event_id": event.id,
            "visual_capture": visual_metadata,
        }

    @app.post("/v1/visual/direct/messages", status_code=202)
    def accept_direct_visual_message(req: DirectVisualMessageRequest):
        ensure_character(req.character_id)
        try:
            frame_urls, visual_metadata = normalize_visual_frames(req.visual_frames)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        now = req.at or datetime.now().astimezone()
        event = build_user_event(
            req.message,
            character_id=req.character_id,
            conversation_id=req.conversation_id,
            at=now,
        )
        event.content = f"{event.content}\n[实时视觉：本轮同时提供 {len(frame_urls)} 张按时间顺序采集的摄像头/屏幕关键帧，请结合图像本体理解。]".strip()
        event.metadata["display_text"] = req.message.strip()
        presentation = bind_presentation(getattr(app.state, "live2d_root", None), req.live2d, [req.character_id])
        if presentation:
            event.metadata["live2d"] = presentation
        event.metadata["visual_capture"] = visual_metadata
        event = access.store().append_event(event)
        scheduler().enqueue_direct(
            req.character_id,
            req.conversation_id,
            event,
            image_data_urls=frame_urls,
        )
        logger.info(
            "visual.capture direct character=%s conversation=%s event_id=%s frames=%d sources=%s",
            req.character_id,
            req.conversation_id,
            event.id,
            len(frame_urls),
            visual_metadata.get("sources"),
        )
        return {
            "accepted": True,
            "event_id": event.id,
            "visual_capture": visual_metadata,
            "message": {
                "id": event.id,
                "role": "user",
                "character_id": event.character_id,
                "content": req.message.strip(),
                "event_time": event.event_time.isoformat(),
                "source_event_id": event.id,
                "has_trace": False,
            },
        }

    @app.post("/v1/visual/groups/{conversation_id}/messages", status_code=202)
    def accept_group_visual_message(conversation_id: str, req: GroupVisualMessageRequest):
        store = access.store()
        repository = GroupRepository(store)
        group = repository.get_group(conversation_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        try:
            frame_urls, visual_metadata = normalize_visual_frames(req.visual_frames)
            mentions = resolve_group_mentions(group.member_ids, req.message, profiles_by_id(), req.mentions)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        now = req.at or datetime.now().astimezone()
        event = build_group_user_event(
            conversation_id,
            req.message,
            at=now,
            mentions=mentions,
        )
        event.content = f"{event.content}\n[实时视觉：本轮同时提供 {len(frame_urls)} 张按时间顺序采集的摄像头/屏幕关键帧，请结合图像本体理解。]".strip()
        event.metadata["visual_capture"] = visual_metadata
        try:
            presentation = bind_presentation(getattr(app.state, "live2d_root", None), req.live2d, group.member_ids)
            if presentation:
                event.metadata["live2d"] = presentation
            event = repository.append_user_event(event)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="group not found") from exc
        scheduler().enqueue_group(conversation_id, event, image_data_urls=frame_urls)
        logger.info(
            "visual.capture group conversation=%s event_id=%s frames=%d sources=%s mentions=%s",
            conversation_id,
            event.id,
            len(frame_urls),
            visual_metadata.get("sources"),
            mentions,
        )
        return {
            "accepted": True,
            "event_id": event.id,
            "turn_id": event.turn_id,
            "visual_capture": visual_metadata,
        }

    return app
