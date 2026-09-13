from __future__ import annotations

import base64
import logging
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from character_memory.application.chat_service import build_user_event
from character_memory.application.group_conversation_service import build_group_user_event, resolve_group_mentions
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
    message: str = Field(min_length=1, max_length=12000)
    visual_frames: list[VisualFrameRequest] = Field(min_length=1, max_length=5)
    character_id: str = "rin"
    conversation_id: str = "default"
    at: datetime | None = None


class GroupVisualMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    visual_frames: list[VisualFrameRequest] = Field(min_length=1, max_length=5)
    mentions: list[str] = Field(default_factory=list, max_length=4)
    at: datetime | None = None


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
        event = repository.append_event(event)
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
