from __future__ import annotations

from datetime import datetime
import hashlib
import logging
import os
import socket
import ipaddress
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field, model_validator

from character_memory.space_store import MAX_IMAGES_PER_POST, SpaceAttachmentInput
from character_memory.visual_generation import (
    ImageGenerationRequest,
    VisualPromptPlanner,
    VisualPurpose,
    visual_aspect_ratio,
)
from character_memory.world_observation import WorldObservation, WorldObservationBundle, WorldObservationService


logger = logging.getLogger("character_memory.space_media")


class SpaceObservationDecision(BaseModel):
    should_explore: bool = False
    query: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def normalize(self):
        self.query = self.query.strip()
        if self.should_explore and not self.query:
            self.should_explore = False
        return self


class SpaceMediaIntent(BaseModel):
    kind: Literal["SEARCH_IMAGE", "GENERATE_IMAGE", "VOICE", "LINK_PREVIEW"]
    query: str = Field(default="", max_length=280)
    prompt: str = Field(default="", max_length=800)
    text: str = Field(default="", max_length=2400)
    count: int = Field(default=1, ge=1, le=MAX_IMAGES_PER_POST)
    purpose: Literal["SELFIE", "SCENE"] = "SCENE"
    observation_index: int = Field(default=0, ge=0, le=7)

    @model_validator(mode="after")
    def validate_payload(self):
        self.query = self.query.strip()
        self.prompt = self.prompt.strip()
        self.text = self.text.strip()
        if self.kind == "SEARCH_IMAGE" and not self.query:
            raise ValueError("SEARCH_IMAGE requires query")
        if self.kind == "GENERATE_IMAGE" and not self.prompt:
            raise ValueError("GENERATE_IMAGE requires prompt")
        if self.kind == "VOICE" and not self.text:
            raise ValueError("VOICE requires text")
        return self


class SpacePostPlan(BaseModel):
    should_post: bool = False
    text: str = Field(default="", max_length=4000)
    media: list[SpaceMediaIntent] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def normalize(self):
        self.text = self.text.strip()
        if not self.should_post:
            self.text = ""
            self.media = []
        return self


class SpaceMediaExecution(BaseModel):
    attachments: list[SpaceAttachmentInput] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    fallback_text: str = ""


def deterministic_gate(character_id: str, now: datetime, label: str, chance: float) -> bool:
    chance = max(0.0, min(1.0, float(chance)))
    if chance <= 0:
        return False
    if chance >= 1:
        return True
    token = f"{character_id}:{now.isoformat(timespec='minutes')}:{label}".encode("utf-8")
    value = int.from_bytes(hashlib.sha256(token).digest()[:8], "big") / float(2**64 - 1)
    return value < chance


class SpaceMediaExecutor:
    """Resolve a character-authored media plan into durable Space attachments.

    Provider/network failure is isolated per intent. Callers may still publish
    the textual part of the plan instead of turning an image/TTS outage into a
    failed Space opportunity.
    """

    def __init__(
        self,
        access,
        *,
        observation_service: WorldObservationService | None = None,
        http_client: httpx.Client | None = None,
        media_base_url: str | None = None,
    ):
        self.access = access
        self.settings = access.settings
        self.observation_service = observation_service or WorldObservationService(self.settings)
        self._owns_client = http_client is None
        self.client = http_client or httpx.Client(timeout=40.0, follow_redirects=False)
        self.media_base_url = (
            media_base_url
            or os.getenv("CHARACTER_MEDIA_BASE_URL", "http://127.0.0.1:8001")
        ).rstrip("/")

    @staticmethod
    def _validate_public_url(url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("media download requires public http/https URL")
        try:
            infos = socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError("media host cannot be resolved") from exc
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise ValueError("media download rejected a non-public address")
        return parsed.geturl()

    def _download_image(self, character_id: str, url: str, now: datetime, label: str):
        current = self._validate_public_url(url)
        response = None
        for _ in range(4):
            response = self.client.get(
                current,
                headers={
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9,*/*;q=0.1",
                    "User-Agent": "character-memory/0.5 space-image",
                },
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = str(response.headers.get("location") or "").strip()
                if not location:
                    raise RuntimeError("image redirect is missing Location")
                current = self._validate_public_url(urljoin(current, location))
                continue
            break
        if response is None or response.is_error:
            status = getattr(response, "status_code", "unknown")
            raise RuntimeError(f"image download failed with HTTP {status}")
        payload = bytes(response.content or b"")
        asset = self.access.media_storage.save_bytes(
            character_id=character_id,
            original_name=label or "space-search-image",
            payload=payload,
            created_at=now,
            source="SPACE_SEARCH_IMAGE",
        )
        self.access.store().add_media_asset(asset)
        return asset

    @staticmethod
    def _recent_dialogue(runtime, character_id: str, before: datetime) -> list[str]:
        result: list[str] = []
        for item in runtime.store.list_chat_events(character_id, limit=8):
            if item.event_time > before:
                continue
            text = " ".join(str(item.content or "").split()).strip()
            if text:
                result.append(text[:360])
        return result[-8:]

    def _generate_image(
        self,
        character_id: str,
        runtime,
        intent: SpaceMediaIntent,
        now: datetime,
    ):
        visual_runtime = getattr(self.access, "visual_runtime", None)
        if visual_runtime is None:
            raise RuntimeError("ImageGen runtime is not attached")
        provider_name, provider = visual_runtime._provider()
        if provider is None:
            raise RuntimeError(f"ImageGen provider unavailable: {provider_name}")

        purpose = VisualPurpose(intent.purpose)
        reference = None
        if purpose == VisualPurpose.SELFIE and provider.supports_reference_images:
            reference = visual_runtime._avatar_reference(character_id)
        state = runtime.store.get_mental_state(character_id, at=now)
        prompt = VisualPromptPlanner(runtime.model).compile_prompt(
            character_id,
            purpose=purpose,
            persona=runtime.persona,
            mental_state=state,
            recent_dialogue=self._recent_dialogue(runtime, character_id, now),
            visual_intent=intent.prompt,
            has_reference_image=bool(reference),
        )
        result = provider.generate(
            ImageGenerationRequest(
                prompt=prompt,
                aspect_ratio=visual_aspect_ratio(purpose),
                size="1K",
                reference_images=[reference] if reference else [],
            )
        )
        suffix = str(result.mime_type or "image/png").split("/", 1)[-1] or "png"
        asset = self.access.media_storage.save_bytes(
            character_id=character_id,
            original_name=f"space-generated-{purpose.value.lower()}.{suffix}",
            payload=result.payload,
            created_at=now,
            source=f"SPACE_GENERATED_{purpose.value}",
        )
        self.access.store().add_media_asset(asset)
        return asset, result

    def _voice(self, character_id: str, text: str, now: datetime):
        response = self.client.post(
            f"{self.media_base_url}/v1/tts",
            json={"text": text},
            timeout=180.0,
        )
        if response.is_error:
            raise RuntimeError(f"TTS failed with HTTP {response.status_code}: {(response.text or '')[:300]}")
        asset = self.access.media_storage.save_bytes(
            character_id=character_id,
            original_name="space-voice",
            payload=response.content,
            created_at=now,
            source="SPACE_TTS",
        )
        self.access.store().add_media_asset(asset)
        duration = str(response.headers.get("x-media-audio-ms") or "").strip()
        try:
            duration_ms = int(float(duration)) if duration else None
        except ValueError:
            duration_ms = None
        return asset, duration_ms

    def resolve(
        self,
        *,
        character_id: str,
        runtime,
        plan: SpacePostPlan,
        observations: WorldObservationBundle | None,
        now: datetime,
        max_images: int,
    ) -> SpaceMediaExecution:
        attachments: list[SpaceAttachmentInput] = []
        errors: list[str] = []
        fallback_text = ""
        image_budget = max(0, min(MAX_IMAGES_PER_POST, int(max_images)))

        for intent in plan.media:
            try:
                if intent.kind == "LINK_PREVIEW":
                    values = observations.observations if observations is not None else []
                    if not values:
                        raise ValueError("no web observation is available for link preview")
                    index = min(intent.observation_index, len(values) - 1)
                    item = values[index]
                    attachments.append(
                        SpaceAttachmentInput(
                            kind="LINK_PREVIEW",
                            source="WEB",
                            url=item.url,
                            title=item.title,
                            description=item.snippet,
                            thumbnail_url=item.thumbnail_url or None,
                            metadata={"query": observations.query, "source_domain": item.source_domain},
                        )
                    )
                    continue

                if intent.kind == "VOICE":
                    asset, duration_ms = self._voice(character_id, intent.text, now)
                    attachments.append(
                        SpaceAttachmentInput(
                            kind="AUDIO",
                            source="TTS",
                            media_id=asset.id,
                            transcript=intent.text,
                            duration_ms=duration_ms,
                        )
                    )
                    fallback_text = fallback_text or intent.text
                    continue

                remaining = image_budget - sum(1 for item in attachments if item.kind == "IMAGE")
                if remaining <= 0:
                    continue
                count = min(intent.count, remaining)

                if intent.kind == "SEARCH_IMAGE":
                    candidates = self.observation_service.search_images(intent.query, limit=count)
                    for index, candidate in enumerate(candidates[:count]):
                        try:
                            asset = self._download_image(
                                character_id,
                                candidate.image_url,
                                now,
                                f"space-search-{index + 1}.jpg",
                            )
                            attachments.append(
                                SpaceAttachmentInput(
                                    kind="IMAGE",
                                    source="SEARCH",
                                    media_id=asset.id,
                                    title=candidate.title,
                                    url=candidate.source_page_url,
                                    metadata={
                                        "query": intent.query,
                                        "source_domain": candidate.source_domain,
                                        "source_page_url": candidate.source_page_url,
                                    },
                                )
                            )
                        except Exception as exc:
                            errors.append(f"SEARCH_IMAGE[{index}]: {exc}")
                    continue

                if intent.kind == "GENERATE_IMAGE":
                    for index in range(count):
                        try:
                            asset, result = self._generate_image(character_id, runtime, intent, now)
                            attachments.append(
                                SpaceAttachmentInput(
                                    kind="IMAGE",
                                    source="GENERATED",
                                    media_id=asset.id,
                                    title="AI 生成配图",
                                    metadata={
                                        "purpose": intent.purpose,
                                        "provider": result.provider,
                                        "model": result.model,
                                        "visual_intent": intent.prompt,
                                        "index": index,
                                    },
                                )
                            )
                        except Exception as exc:
                            errors.append(f"GENERATE_IMAGE[{index}]: {exc}")
                    continue
            except Exception as exc:
                errors.append(f"{intent.kind}: {exc}")

        return SpaceMediaExecution(
            attachments=attachments,
            errors=errors,
            fallback_text=fallback_text,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
        self.observation_service.close()
