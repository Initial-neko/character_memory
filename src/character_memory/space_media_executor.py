from __future__ import annotations

import base64
from datetime import datetime
import logging
from pathlib import Path
from typing import Any

from character_memory.domain.models import EventType, SpaceMediaIntent, SpaceMediaIntentType
from character_memory.remote_media import RemoteMediaFetcher
from character_memory.visual_generation import (
    ImageGenerationRequest,
    VisualPromptPlanner,
    VisualPurpose,
    visual_aspect_ratio,
)


logger = logging.getLogger("character_memory.space_media_executor")


class SpaceMediaExecutor:
    """Execute optional Space image intents into durable MediaAssets.

    The planner decides whether media is natural for the post. This class does
    not make social decisions; it only executes SEARCH_IMAGE / GENERATE_IMAGE
    requests and returns ordered Space attachment relations. Every intent is
    fail-soft so text publication is never coupled to an external media provider.
    """

    def __init__(
        self,
        access,
        *,
        search_provider=None,
        remote_fetcher: RemoteMediaFetcher | None = None,
        image_providers: dict[str, Any] | None = None,
    ):
        self.access = access
        self.settings = access.settings
        self._search_provider = search_provider
        self._image_providers = image_providers
        self._remote_fetcher = remote_fetcher

    def max_items(self) -> int:
        return max(0, min(9, int(getattr(self.settings, "space_media_max_items", 3))))

    def enabled(self) -> bool:
        return bool(getattr(self.settings, "space_media_enabled", True)) and self.max_items() > 0

    def _search(self):
        if self._search_provider is not None:
            return self._search_provider
        avatar_search = getattr(self.access, "avatar_search", None)
        return getattr(avatar_search, "provider", None) if avatar_search is not None else None

    def _fetcher(self) -> RemoteMediaFetcher:
        if self._remote_fetcher is None:
            self._remote_fetcher = RemoteMediaFetcher(
                max_bytes=int(getattr(self.access.media_storage, "max_bytes", 8 * 1024 * 1024)),
            )
        return self._remote_fetcher

    def _providers(self) -> dict[str, Any]:
        if self._image_providers is not None:
            return self._image_providers
        return getattr(self.access, "image_generation_providers", {}) or {}

    @staticmethod
    def _extension(mime_type: str) -> str:
        return {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
        }.get(str(mime_type or "").lower(), "png")

    def _save_asset(
        self,
        character_id: str,
        payload: bytes,
        *,
        mime_type: str,
        now: datetime,
        source: str,
        label: str,
    ):
        extension = self._extension(mime_type)
        asset = self.access.media_storage.save_bytes(
            character_id=character_id,
            original_name=f"{label}.{extension}",
            payload=payload,
            created_at=now,
            source=source,
        )
        try:
            self.access.store().add_media_asset(asset)
        except Exception:
            self.access.media_storage.delete(asset)
            raise
        return asset

    def _search_images(
        self,
        character_id: str,
        intent: SpaceMediaIntent,
        *,
        now: datetime,
        remaining: int,
    ) -> list[dict[str, Any]]:
        if not bool(getattr(self.settings, "space_image_search_enabled", True)):
            raise RuntimeError("Space image search is disabled")
        provider = self._search()
        if provider is None:
            raise RuntimeError("image search provider is unavailable")
        query = str(intent.query or "").strip()
        target = min(remaining, max(1, int(intent.count)))
        results = provider.search_images(query, limit=min(30, max(target * 3, target)))
        relations: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        errors: list[str] = []

        for candidate in results:
            if len(relations) >= target:
                break
            urls = [candidate.image_url, candidate.thumbnail_url]
            remote = None
            for raw_url in urls:
                url = str(raw_url or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                try:
                    remote = self._fetcher().fetch_image(url)
                    break
                except (RuntimeError, ValueError) as exc:
                    errors.append(str(exc))
            if remote is None:
                continue

            asset = self._save_asset(
                character_id,
                remote.payload,
                mime_type=remote.content_type,
                now=now,
                source="SPACE_SEARCH_IMAGE",
                label="space-search",
            )
            relations.append(
                {
                    "media_id": asset.id,
                    "media_type": "IMAGE",
                    "source_type": "SEARCH",
                    "metadata": {
                        "query": query,
                        "title": candidate.title,
                        "source_page_url": candidate.source_page_url,
                        "source_image_url": remote.source_url,
                        "source_domain": candidate.source_domain,
                        "width": candidate.width,
                        "height": candidate.height,
                    },
                }
            )

        if not relations:
            detail = errors[-1] if errors else "search returned no downloadable images"
            raise RuntimeError(detail)
        return relations

    @staticmethod
    def _recent_dialogue(runtime, character_id: str, before: datetime) -> list[str]:
        events = runtime.store.list_events(character_id, limit=10, before=before)[-8:]
        lines: list[str] = []
        for item in events:
            if item.event_type == EventType.USER_MESSAGE:
                role = "用户"
                content = item.metadata.get("display_text", item.content)
            elif item.event_type == EventType.CHARACTER_MESSAGE:
                role = "角色"
                content = item.content
            else:
                continue
            text = " ".join(str(content or "").split()).strip()
            if text:
                lines.append(f"{role}: {text[:360]}")
        return lines

    def _avatar_reference(self, character_id: str) -> str | None:
        avatar_store = getattr(self.access, "avatar_store", None)
        if avatar_store is None:
            return None
        path = avatar_store.asset_path(character_id)
        metadata = avatar_store.load(character_id)
        if path is None or metadata is None:
            return None
        return f"data:{metadata.content_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

    def _generate_images(
        self,
        character_id: str,
        intent: SpaceMediaIntent,
        *,
        now: datetime,
        remaining: int,
        runtime=None,
    ) -> list[dict[str, Any]]:
        if not bool(getattr(self.settings, "space_image_generation_enabled", True)):
            raise RuntimeError("Space image generation is disabled")
        providers = self._providers()
        provider_name = str(
            getattr(self.settings, "image_generation_provider", "agnes") or "agnes"
        ).strip().lower()
        provider = providers.get(provider_name)
        if provider is None or not provider.available():
            raise RuntimeError(f"image generation provider unavailable: {provider_name}")

        if runtime is None:
            bundle = self.access.require_bundle()
            runtime = getattr(bundle, "runtimes", {}).get(character_id)
        if runtime is None:
            raise RuntimeError(f"runtime not found for character: {character_id}")

        try:
            purpose = VisualPurpose(str(intent.purpose or "SCENE").strip().upper())
        except ValueError as exc:
            raise ValueError(f"unsupported Space image purpose: {intent.purpose}") from exc
        if purpose not in {VisualPurpose.SELFIE, VisualPurpose.SCENE}:
            raise ValueError("Space generated images must use SELFIE or SCENE")

        reference = None
        if purpose == VisualPurpose.SELFIE and provider.supports_reference_images:
            reference = self._avatar_reference(character_id)

        mental_state = runtime.store.get_mental_state(character_id, at=now)
        prompt = VisualPromptPlanner(runtime.model).compile_prompt(
            character_id,
            purpose=purpose,
            persona=runtime.persona,
            mental_state=mental_state,
            recent_dialogue=self._recent_dialogue(runtime, character_id, now),
            visual_intent=str(intent.visual_intent or "").strip(),
            has_reference_image=bool(reference),
        )

        target = min(remaining, max(1, int(intent.count)))
        relations: list[dict[str, Any]] = []
        for index in range(target):
            result = provider.generate(
                ImageGenerationRequest(
                    prompt=prompt,
                    aspect_ratio=visual_aspect_ratio(purpose),
                    size="1K",
                    reference_images=[reference] if reference else [],
                )
            )
            asset = self._save_asset(
                character_id,
                result.payload,
                mime_type=result.mime_type,
                now=now,
                source=f"SPACE_GENERATED_{purpose.value}",
                label=f"space-generated-{purpose.value.lower()}-{index + 1}",
            )
            relations.append(
                {
                    "media_id": asset.id,
                    "media_type": "IMAGE",
                    "source_type": "GENERATED",
                    "metadata": {
                        "purpose": purpose.value,
                        "visual_intent": intent.visual_intent,
                        "provider": result.provider,
                        "model": result.model,
                        "prompt": prompt,
                        "used_avatar_reference": bool(reference),
                    },
                }
            )
        return relations

    def execute(
        self,
        character_id: str,
        intents: list[SpaceMediaIntent],
        *,
        now: datetime,
        runtime=None,
    ) -> dict[str, Any]:
        if not self.enabled() or not intents:
            return {"relations": [], "errors": []}

        relations: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for intent in intents:
            remaining = self.max_items() - len(relations)
            if remaining <= 0:
                break
            try:
                if intent.type == SpaceMediaIntentType.SEARCH_IMAGE:
                    created = self._search_images(
                        character_id,
                        intent,
                        now=now,
                        remaining=remaining,
                    )
                elif intent.type == SpaceMediaIntentType.GENERATE_IMAGE:
                    created = self._generate_images(
                        character_id,
                        intent,
                        now=now,
                        remaining=remaining,
                        runtime=runtime,
                    )
                else:
                    continue
                relations.extend(created[:remaining])
            except Exception as exc:
                logger.warning(
                    "space.media intent_failed character=%s type=%s error=%s",
                    character_id,
                    intent.type.value,
                    exc,
                )
                errors.append({"type": intent.type.value, "error": str(exc)[:600]})

        return {"relations": relations[: self.max_items()], "errors": errors}

    def discard(self, relations: list[dict[str, Any]]) -> None:
        for relation in relations:
            media_id = str(relation.get("media_id") or "").strip()
            if not media_id:
                continue
            asset = self.access.store().get_media_asset(media_id)
            if asset is None:
                continue
            try:
                self.access.store().delete_media_asset(media_id)
            finally:
                self.access.media_storage.delete(asset)

    def close(self) -> None:
        if self._remote_fetcher is not None:
            self._remote_fetcher.close()
