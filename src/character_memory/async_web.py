from __future__ import annotations
from character_memory.live2d_behavior import bind_presentation

import asyncio
from datetime import datetime
from typing import Annotated
from pydantic import Field
from character_memory.storage.message_receipts import MessageReceiptRepository, ReceiptConflict
from character_memory.storage.reaction_delivery import ReactionDeliveryRepository
from character_memory.domain.facts import FactRef
from character_memory.application.reaction_delivery import DurableReactionDelivery
import json
import os
import logging
import time

from character_memory.life_conversation import prepare_life_message
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
from character_memory.message_projection import upload_caption, client_request_fields
from character_memory.web_lifecycle import background_services


MessageRequestId = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"\S")]


class AsyncChatRequest(ChatRequest):
    request_id: MessageRequestId | None = None


class AsyncGroupChatRequest(GroupChatRequest):
    request_id: MessageRequestId | None = None


logger = logging.getLogger("character_memory.async_web")


async def _stream_hub_events(
    hub: ConversationEventHub,
    channel_key: str,
    *,
    after_id: int = 0,
    initial_event: tuple[str, dict] | None = None,
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
    asyncio sleep. Starlette remains the single owner of ASGI disconnect
    handling, while cancellation can now propagate immediately into this async
    iterator. Durable chat state is still stored in SQLite; this stream remains
    only a low-latency notification channel.
    """
    with hub.channel_lease(channel_key) as channel:
        cursor = max(0, int(after_id or 0))
        next_heartbeat = time.monotonic() + max(0.1, heartbeat_seconds)
        yield "retry: 1500\n\n"

        # Brand-new UI streams skip old ephemeral events by design, but they still
        # need one authoritative status snapshot. Otherwise a locally cached typing
        # flag can survive a tab/character/group switch forever after missing idle.
        if initial_event is not None:
            event_type, data = initial_event
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            yield f"event: {event_type}\ndata: {payload}\n\n"

        while not hub._closed.is_set():
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
    from fastapi import Header, HTTPException
    from fastapi.responses import StreamingResponse

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before async routes are attached")

    hub = ConversationEventHub()
    from character_memory.application.voice_message_materializer import VoiceMessageMaterializer
    materializer = VoiceMessageMaterializer(
        access.store,
        access.media_storage,
        hub,
        media_base=os.getenv("CHARACTER_MEDIA_BASE", "http://127.0.0.1:8001"),
    )
    scheduler = ReactionScheduler(access.get_bundle, access.character_profiles, hub, voice_materializer=materializer)
    deliveries = ReactionDeliveryRepository(access.store())
    delivery = DurableReactionDelivery(deliveries, access.media_storage, scheduler, access.character_profiles)
    scheduler.delivery = delivery
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
            item = catalog.historical_get(sticker_id)
            if item is not None:
                sticker = {
                    **item.model_dump(mode="json"),
                    "url": f"/v1/stickers/{item.id}/asset",
                }
        image_payload = None
        if image:
            image_payload = {
                "id": image.get("id"),
                "label": upload_caption(image.get("original_name")),
                "mime_type": image.get("mime_type"),
                "size_bytes": image.get("size_bytes"),
                "source": image.get("source"),
                "url": f"/v1/media/{image.get('id')}",
            }
        return {
            "id": event.id,
            "role": "user",
            **client_request_fields(event.metadata),
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
            item = catalog.historical_get(sticker_id)
            if item is not None:
                sticker = {
                    **item.model_dump(mode="json"),
                    "url": f"/v1/stickers/{item.id}/asset",
                }
        image_payload = None
        if image:
            image_payload = {
                "id": image.get("id"),
                "label": upload_caption(image.get("original_name")),
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
            **client_request_fields(event.metadata),
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

    def persist_direct_message(req: AsyncChatRequest, rollback_callbacks=None):
        ensure_character(req.character_id)
        selected_sticker = sticker_for(req.sticker_id)
        store = active_store()
        reference = None
        message = req.message
        if req.life_reference:
            ensure_character(req.life_reference.character_id)
            profile = profiles_by_id()[req.life_reference.character_id]
            if profile and "archived_at" in profile:
                raise HTTPException(409, "人物已归档，请先恢复")
            message, reference = prepare_life_message(store, req.life_reference, message, character_id=req.character_id, conversation_id=req.conversation_id)
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
                if rollback_callbacks is not None:
                    rollback_callbacks.append(lambda: access.media_storage.delete(asset))
                store.add_media_asset(asset)
                if rollback_callbacks is not None:
                    rollback_callbacks.append(lambda: store.delete_media_asset(asset.id))
                selected_image = asset.model_dump(mode="json")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        event = build_user_event(
            message,
            character_id=req.character_id,
            conversation_id=req.conversation_id,
            at=now,
            sticker=selected_sticker,
            image=selected_image,
        )
        if req.request_id is not None:
            event.metadata["client_request_id"] = req.request_id
        # Presentation is auxiliary to the immutable user fact, and resource names
        # are derived on the server instead of trusting browser-supplied lists.
        presentation = bind_presentation(getattr(app.state, "live2d_root", None), req.live2d, [req.character_id])
        if presentation:
            event.metadata["live2d"] = presentation
        with store.transaction(immediate=True):
            if reference:
                event.content, reference = prepare_life_message(store, req.life_reference, req.message, character_id=req.character_id, conversation_id=req.conversation_id)
                event.metadata["life_reference"] = reference
            event = store.append_event(event)
            deliveries.record(FactRef(kind="EVENT", id=event.id), direct_channel(req.character_id, req.conversation_id),
                character_id=req.character_id, conversation_id=req.conversation_id,
                media_ids=[event.metadata["media_id"]] if event.metadata.get("media_id") else [])
        response = {
            "accepted": True,
            "event_id": event.id,
            "message": direct_user_payload(event, selected_image),
        }

        def dispatch():
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
        return response, dispatch

    def persist_group_message(conversation_id: str, req: AsyncGroupChatRequest, rollback_callbacks=None):
        store = active_store()
        repository = GroupRepository(store)
        group = repository.get_group(conversation_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        if len(group.member_ids) < 2:
            raise HTTPException(status_code=409, detail="群聊还在构建中，请先完成成员确认。")
        reference = None
        message = req.message
        if req.life_reference:
            ensure_character(req.life_reference.character_id)
            profile = profiles_by_id()[req.life_reference.character_id]
            if profile and "archived_at" in profile:
                raise HTTPException(409, "人物已归档，请先恢复")
            message, reference = prepare_life_message(store, req.life_reference, message, conversation_id=conversation_id)
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
                if rollback_callbacks is not None:
                    rollback_callbacks.append(lambda: access.media_storage.delete(asset))
                store.add_media_asset(asset)
                if rollback_callbacks is not None:
                    rollback_callbacks.append(lambda: store.delete_media_asset(asset.id))
                selected_image = asset.model_dump(mode="json")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        event = build_group_user_event(
            conversation_id,
            message,
            at=now,
            image=selected_image,
            sticker=selected_sticker,
            mentions=mentions,
        )
        if req.request_id is not None:
            event.metadata["client_request_id"] = req.request_id
        try:
            presentation = bind_presentation(getattr(app.state, "live2d_root", None), req.live2d, group.member_ids)
            if presentation:
                event.metadata["live2d"] = presentation
            with store.transaction(immediate=True):
                if reference:
                    event.content, reference = prepare_life_message(store, req.life_reference, req.message, conversation_id=conversation_id)
                    event.metadata["life_reference"] = reference
                event = repository.append_user_event(event)
                deliveries.record(FactRef(kind="CONVERSATION_EVENT", id=event.id, conversation_id=conversation_id),
                    group_channel(conversation_id), conversation_id=conversation_id,
                    media_ids=[event.metadata["media_id"]] if event.metadata.get("media_id") else [], mentions=mentions)
        except KeyError as exc:
            if selected_image is not None:
                store.delete_media_asset(asset.id)
                access.media_storage.delete(asset)
            raise HTTPException(status_code=404, detail="group not found") from exc
        response = {
            "accepted": True,
            "event_id": event.id,
            "turn_id": event.turn_id,
            "message": group_user_payload(event, selected_image),
        }

        def dispatch():
            scheduler.enqueue_group(conversation_id, event, image_data_url=image_data_url)
            logger.info("async.accept group conversation=%s event_id=%s mentions=%s", conversation_id, event.id, mentions)
        return response, dispatch

    def accept_message(scope, req, persist, *, source_kind, conversation_id=None):
        if req.request_id is None:
            cleanup = []
            try:
                response, dispatch = persist(cleanup)
            except BaseException:
                for rollback in cleanup:
                    rollback()
                raise
            dispatch()
            return response
        store = active_store()
        repository = MessageReceiptRepository(store)
        cleanup, dispatches = [], []
        def create():
            response, dispatch = persist(cleanup)
            dispatches.append(dispatch)
            return response
        try:
            accepted = repository.accept(scope, req.request_id,
                req.model_dump(mode="json", exclude={"request_id"}), create,
                source_kind=source_kind, conversation_id=conversation_id)
        except BaseException as exc:
            for rollback in cleanup:
                try:
                    rollback()
                except OSError:
                    logger.exception("async.accept media rollback failed")
            if isinstance(exc, ReceiptConflict):
                raise HTTPException(409, str(exc)) from exc
            raise
        if accepted.created:
            try:
                dispatches[0]()
            except BaseException:
                repository.mark_dispatch(scope, req.request_id, "UNKNOWN")
                raise
            repository.mark_dispatch(scope, req.request_id, "ENQUEUED")
            accepted.dispatch = "ENQUEUED"
        return {**accepted.response, "request_receipt": {
            "request_id": req.request_id, "replayed": not accepted.created,
            "acceptance": "ACCEPTED", "dispatch": accepted.dispatch,
            "execution": deliveries.status(FactRef(kind=source_kind, id=accepted.response["event_id"],
                conversation_id=conversation_id)),
        }}

    @app.post("/v1/chat/messages", status_code=202)
    def accept_direct_message(req: AsyncChatRequest):
        ensure_character(req.character_id)
        return accept_message(["DIRECT", req.character_id, req.conversation_id], req,
            lambda cleanup: persist_direct_message(req, cleanup), source_kind="EVENT")

    @app.post("/v1/groups/{conversation_id}/messages", status_code=202)
    def accept_group_message(conversation_id: str, req: AsyncGroupChatRequest):
        # Group schema must be initialized outside the fact+receipt transaction.
        group = GroupRepository(active_store()).get_group(conversation_id)
        if group is None:
            raise HTTPException(404, "group not found")
        return accept_message(["GROUP", conversation_id], req,
            lambda cleanup: persist_group_message(conversation_id, req, cleanup),
            source_kind="CONVERSATION_EVENT", conversation_id=conversation_id)

    @app.get("/v1/events/stream")
    async def event_stream(
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
        initial_event = None
        if raw_last_id is None:
            # A brand-new UI stream has already reconciled durable history, so
            # skip old ephemeral events but immediately send the scheduler's
            # current state. This re-synchronizes a stale local "typing" flag
            # after character/group/tab switches without replaying old chatter.
            hub_channel = hub._channel(channel)
            with hub_channel.condition:
                last_id = hub_channel.next_id - 1
            initial_event = ("reaction_status", scheduler.status_snapshot(channel))
        else:
            try:
                last_id = int(raw_last_id or 0)
            except ValueError:
                last_id = 0

        return StreamingResponse(
            _stream_hub_events(hub, channel, after_id=last_id, initial_event=initial_event),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def _shutdown_async_runtime():
        scheduler.close()
        hub.close()

    # ReactionScheduler has no start: it brings up per-key threads lazily, so it
    # only has an end. Registering here, before wake_web attaches, is what puts
    # the wake producer ahead of this hub in the stop order.
    background_services(app).register("async_reactions", stop=_shutdown_async_runtime)
    background_services(app).register("reaction_delivery", start=delivery.start, stop=delivery.stop)

    return app
