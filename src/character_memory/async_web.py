from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import time

from character_memory.api import ChatRequest
from character_memory.application.async_conversation import (
    ConversationEventHub,
    ReactionScheduler,
    direct_channel,
    group_channel,
)
from character_memory.application.chat_service import build_user_event
from character_memory.application.group_conversation_service import build_group_user_event, resolve_group_mentions
from character_memory.group_store import GroupRepository
from character_memory.group_web import GroupChatRequest


logger = logging.getLogger("character_memory.async_web")


async def _stream_hub_events(
    hub: ConversationEventHub,
    channel_key: str,
    *,
    after_id: int = 0,
    is_disconnected=None,
    poll_seconds: float = 0.1,
    heartbeat_seconds: float = 15.0,
):
    """Yield SSE frames without blocking an AnyIO worker thread.

    The previous implementation delegated a synchronous generator to
    ``StreamingResponse``. That generator waited on ``threading.Condition`` for
    up to 15 seconds. During Uvicorn shutdown the response task could be
    cancelled, but the worker thread running ``next()`` remained blocked, so
    Ctrl+C waited indefinitely for the active SSE request to finish.

    This loop only performs short, lock-protected snapshots and then awaits an
    asyncio sleep. Cancellation therefore propagates immediately when Uvicorn
    closes active HTTP connections. Durable chat state is still stored in
    SQLite; this stream remains only a low-latency notification channel.
    """
    channel = hub._channel(channel_key)
    cursor = max(0, int(after_id or 0))
    next_heartbeat = time.monotonic() + max(0.1, heartbeat_seconds)
    yield "retry: 1500\n\n"

    while not hub._closed.is_set():
        if is_disconnected is not None and await is_disconnected():
            return

        with channel.condition:
            batch = [item for item in channel.events if item[0] > cursor]

        if batch:
            for seq, event_type, data in batch:
                cursor = seq
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {seq}\nevent: {event_type}\ndata: {payload}\n\n"
            next_heartbeat = time.monotonic() + max(0.1, heartbeat_seconds)
            # Give cancellation a scheduling point even during an event burst.
            await asyncio.sleep(0)
            continue

        now = time.monotonic()
        if now >= next_heartbeat:
            yield ": ping\n\n"
            next_heartbeat = now + max(0.1, heartbeat_seconds)

        await asyncio.sleep(max(0.01, poll_seconds))


def attach_async_routes(app):
    """Attach non-blocking message acceptance and SSE delivery routes."""
    from fastapi import Header, HTTPException, Request
    from fastapi.responses import StreamingResponse

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before async routes are attached")

    hub = ConversationEventHub()
    scheduler = ReactionScheduler(access.get_bundle, access.character_profiles, hub)
    access.stream_hub = hub
    access.reaction_scheduler = scheduler

    def profiles_by_id() -> dict[str, dict]:
        return {item["id"]: item for item in access.character_profiles()}

    def ensure_character(character_id: str) -> None:
        if character_id not in profiles_by_id():
            raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")

    def active_store():
        return access.store()

    def sticker_for(sticker_id: str | None) -> dict | None:
        if not sticker_id:
            return None
        catalog = access.global_sticker_catalog()
        sticker = catalog.get(sticker_id)
        if sticker is None or catalog.asset_path(sticker_id) is None:
            raise HTTPException(status_code=400, detail=f"Unknown sticker: {sticker_id}")
        return sticker.model_dump(mode="json")

    def direct_user_payload(event, image: dict | None = None) -> dict:
        sticker_id = event.metadata.get("sticker_id")
        sticker = None
        if sticker_id:
            catalog = access.global_sticker_catalog()
            item = catalog.get(sticker_id)
            if item is not None:
                sticker = {
                    **item.model_dump(mode="json"),
                    "url": f"/v1/stickers/{item.id}/asset",
                }
        image_payload = None
        if image:
            image_payload = {
                "id": image.get("id"),
                "label": image.get("original_name") or "图片",
                "mime_type": image.get("mime_type"),
                "size_bytes": image.get("size_bytes"),
                "source": image.get("source"),
                "url": f"/v1/media/{image.get('id')}",
            }
        return {
            "id": event.id,
            "role": "user",
            "character_id": event.character_id,
            "content": event.metadata.get("display_text", event.content),
            "event_time": event.event_time.isoformat(),
            "sticker_id": sticker_id,
            "sticker": sticker,
            "media_id": event.metadata.get("media_id"),
            "image": image_payload,
            "source_event_id": event.id,
            "has_trace": False,
        }

    def group_user_payload(event, image: dict | None = None) -> dict:
        sticker_id = event.metadata.get("sticker_id")
        sticker = None
        if sticker_id:
            catalog = access.global_sticker_catalog()
            item = catalog.get(sticker_id)
            if item is not None:
                sticker = {
                    **item.model_dump(mode="json"),
                    "url": f"/v1/stickers/{item.id}/asset",
                }
        image_payload = None
        if image:
            image_payload = {
                "id": image.get("id"),
                "label": image.get("original_name") or "图片",
                "mime_type": image.get("mime_type"),
                "size_bytes": image.get("size_bytes"),
                "source": image.get("source"),
                "url": f"/v1/media/{image.get('id')}",
            }
        return {
            "id": event.id,
            "conversation_id": event.conversation_id,
            "turn_id": event.turn_id,
            "role": "user",
            "actor_type": "USER",
            "actor_id": "user",
            "actor_name": "我",
            "content": event.metadata.get("display_text", event.content),
            "event_time": event.event_time.isoformat(),
            "action": event.metadata.get("action"),
            "mentions": event.metadata.get("mentions", []),
            "sticker_id": sticker_id,
            "sticker": sticker,
            "media_id": event.metadata.get("media_id"),
            "image": image_payload,
            "turn_summary": None,
        }

    @app.post("/v1/chat/messages", status_code=202)
    def accept_direct_message(req: ChatRequest):
        ensure_character(req.character_id)
        selected_sticker = sticker_for(req.sticker_id)
        store = active_store()
        selected_image = None
        image_data_url = None
        now = req.at or datetime.now().astimezone()
        if req.image is not None:
            try:
                asset, image_data_url = access.media_storage.save_data_url(
                    character_id=req.character_id,
                    original_name=req.image.filename,
                    data_url=req.image.data_url,
                    created_at=now,
                )
                store.add_media_asset(asset)
                selected_image = asset.model_dump(mode="json")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        event = build_user_event(
            req.message,
            character_id=req.character_id,
            conversation_id=req.conversation_id,
            at=now,
            sticker=selected_sticker,
            image=selected_image,
        )
        event = store.append_event(event)
        scheduler.enqueue_direct(
            req.character_id,
            req.conversation_id,
            event,
            image_data_url=image_data_url,
        )
        logger.info(
            "async.accept direct character=%s conversation=%s event_id=%s",
            req.character_id,
            req.conversation_id,
            event.id,
        )
        return {
            "accepted": True,
            "event_id": event.id,
            "message": direct_user_payload(event, selected_image),
        }

    @app.post("/v1/groups/{conversation_id}/messages", status_code=202)
    def accept_group_message(conversation_id: str, req: GroupChatRequest):
        store = active_store()
        repository = GroupRepository(store)
        group = repository.get_group(conversation_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        try:
            mentions = resolve_group_mentions(group.member_ids, req.message, profiles_by_id(), req.mentions)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        selected_sticker = sticker_for(req.sticker_id)
        selected_image = None
        image_data_url = None
        now = req.at or datetime.now().astimezone()
        if req.image is not None:
            try:
                asset, image_data_url = access.media_storage.save_data_url(
                    character_id=f"group:{conversation_id}",
                    original_name=req.image.filename,
                    data_url=req.image.data_url,
                    created_at=now,
                )
                store.add_media_asset(asset)
                selected_image = asset.model_dump(mode="json")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        event = build_group_user_event(
            conversation_id,
            req.message,
            at=now,
            image=selected_image,
            sticker=selected_sticker,
            mentions=mentions,
        )
        event = repository.append_event(event)
        scheduler.enqueue_group(conversation_id, event, image_data_url=image_data_url)
        logger.info("async.accept group conversation=%s event_id=%s mentions=%s", conversation_id, event.id, mentions)
        return {
            "accepted": True,
            "event_id": event.id,
            "turn_id": event.turn_id,
            "message": group_user_payload(event, selected_image),
        }

    @app.get("/v1/events/stream")
    async def event_stream(
        request: Request,
        scope: str,
        conversation_id: str,
        character_id: str | None = None,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        normalized = scope.strip().lower()
        if normalized == "direct":
            if not character_id:
                raise HTTPException(status_code=400, detail="character_id is required for direct stream")
            ensure_character(character_id)
            channel = direct_channel(character_id, conversation_id)
        elif normalized == "group":
            if GroupRepository(active_store()).get_group(conversation_id) is None:
                raise HTTPException(status_code=404, detail="group not found")
            channel = group_channel(conversation_id)
        else:
            raise HTTPException(status_code=400, detail="scope must be direct or group")

        raw_last_id = last_event_id
        if raw_last_id is None:
            # A brand-new UI stream has already reconciled durable state through
            # the history endpoint. Start at the current ephemeral tail so old
            # typing/member-complete notifications are not replayed on tab/group
            # switches. Native EventSource reconnects do send Last-Event-ID.
            hub_channel = hub._channel(channel)
            with hub_channel.condition:
                last_id = hub_channel.next_id - 1
        else:
            try:
                last_id = int(raw_last_id or 0)
            except ValueError:
                last_id = 0

        return StreamingResponse(
            _stream_hub_events(
                hub,
                channel,
                after_id=last_id,
                is_disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def _shutdown_async_runtime():
        scheduler.close()
        hub.close()

    app.router.on_shutdown.insert(0, _shutdown_async_runtime)

    return app
