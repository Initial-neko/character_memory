from __future__ import annotations

from datetime import datetime, timezone
import binascii
import json
import logging
from pathlib import Path
import struct
from typing import Any, Callable
import zlib

import yaml

from character_memory.config import load_persona
from character_memory.persona_builder import PersonaDraft
from character_memory.visual_generation import (
    ImageGenerationRequest,
    VisualPromptPlanner,
    VisualPurpose,
    visual_aspect_ratio,
)
from character_memory.voice_web import write_character_voice
from character_memory.voices import discover_templates, template_root


logger = logging.getLogger("character_memory.character_onboarding")
CREATION_METADATA_FILENAME = "creation.json"


def _creation_path(persona_path: str | Path) -> Path:
    return Path(persona_path).parent / CREATION_METADATA_FILENAME


def load_creation_metadata(persona_path: str | Path) -> dict[str, Any] | None:
    path = _creation_path(persona_path)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.exception("character.creation_metadata invalid path=%s", path)
        return None
    return value if isinstance(value, dict) else None


def save_creation_metadata(
    persona_path: str | Path,
    *,
    creation: dict[str, Any] | None,
    initialization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = load_creation_metadata(persona_path) or {}
    source = str((creation or {}).get("source") or previous.get("source") or "UNKNOWN").strip().upper()
    prompt = str((creation or {}).get("prompt") or previous.get("prompt") or "").strip()
    record = {
        "source": source,
        "prompt": prompt,
        "name_hint": str((creation or {}).get("name_hint") or previous.get("name_hint") or "").strip(),
        "age_hint": (creation or {}).get("age_hint", previous.get("age_hint")),
        "tags": [
            str(item).strip()
            for item in ((creation or {}).get("tags") or previous.get("tags") or [])
            if str(item).strip()
        ][:12],
        "group_id": str((creation or {}).get("group_id") or previous.get("group_id") or "").strip() or None,
        "created_at": previous.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "initialization": initialization if initialization is not None else previous.get("initialization"),
    }
    path = _creation_path(persona_path)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return record


def persona_inspector_payload(profile: dict[str, Any]) -> dict[str, Any]:
    persona_path = Path(profile["persona_path"])
    try:
        raw = yaml.safe_load(persona_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"persona could not be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("persona document must be a mapping")
    creation = load_creation_metadata(persona_path)
    if creation is None:
        creation = {
            "source": "LEGACY",
            "prompt": "",
            "name_hint": "",
            "age_hint": None,
            "tags": [],
            "group_id": None,
            "created_at": None,
            "initialization": None,
        }
    return {
        "character_id": str(profile["id"]),
        "persona": raw,
        "creation": creation,
        "read_only": True,
    }


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)


def _fallback_png(character_id: str, size: int = 128) -> bytes:
    """Return a dependency-free square PNG used only when every real avatar path fails."""

    seed = sum(ord(ch) for ch in character_id) % 96
    rgb = (74 + seed, 90 + seed // 2, 120 + seed // 3)
    row = bytes([0]) + bytes(rgb) * size
    raw = row * size
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


class CharacterOnboardingService:
    """Finish a new Character without making creation depend on asset drawers."""

    def __init__(
        self,
        *,
        settings: Any,
        services: Any,
        current_bundle: Callable[[], Any | None],
    ):
        self.settings = settings
        self.services = services
        self.current_bundle = current_bundle

    def _polished_avatar_prompt(self, profile: dict[str, Any], draft: PersonaDraft) -> str:
        current = self.current_bundle()
        if current is None or not hasattr(current, "model"):
            raise RuntimeError("character runtime model is unavailable")
        persona = load_persona(profile["persona_path"])
        mental_state = None
        store = getattr(current, "store", None)
        if store is not None:
            try:
                mental_state = store.get_mental_state(str(profile["id"]))
            except Exception:
                logger.exception("character.onboarding mental_state_failed character=%s", profile["id"])
        intent = (
            f"为刚创建的角色 {draft.name} 准备第一张长期使用的聊天头像。"
            "单人、正方形头像构图、面部清晰、背景简洁，不要文字、水印或 logo；"
            "保持 Persona 身份与气质一致，适合作为长期聊天头像。"
        )
        return VisualPromptPlanner(current.model).compile_prompt(
            str(profile["id"]),
            purpose=VisualPurpose.AVATAR,
            persona=persona,
            mental_state=mental_state,
            recent_dialogue=[],
            visual_intent=intent,
            has_reference_image=False,
        )

    def _generated_avatar(self, profile: dict[str, Any], draft: PersonaDraft) -> dict[str, Any] | None:
        character_id = str(profile["id"])
        providers = getattr(self.services, "image_generation_providers", {}) or {}
        preferred = str(getattr(self.settings, "image_generation_provider", "") or "").strip()
        ordered = []
        if preferred in providers:
            ordered.append((preferred, providers[preferred]))
        ordered.extend((name, provider) for name, provider in providers.items() if name != preferred)
        if not ordered:
            return None

        try:
            prompt = self._polished_avatar_prompt(profile, draft)
        except Exception as exc:
            logger.warning("character.onboarding prompt_failed character=%s error=%s", character_id, exc)
            return None

        for name, provider in ordered:
            try:
                configured = provider.configured() if hasattr(provider, "configured") else True
                if not configured or not provider.available():
                    continue
                result = provider.generate(
                    ImageGenerationRequest(
                        prompt=prompt,
                        aspect_ratio=visual_aspect_ratio(VisualPurpose.AVATAR),
                        size="1K",
                    )
                )
                metadata = self.services.avatar_store.save_from_bytes(
                    character_id,
                    result.payload,
                    result.mime_type,
                    source="GENERATED_INITIAL",
                    provider=result.provider,
                    model=result.model,
                    title=f"{draft.name} initial avatar",
                    prompt=prompt,
                )
                return {
                    "status": "ready",
                    "source": "image_generation",
                    "provider": result.provider,
                    "model": result.model,
                    "avatar": metadata.model_dump(mode="json"),
                }
            except Exception as exc:
                logger.warning(
                    "character.onboarding avatar_generation_failed character=%s provider=%s error=%s",
                    character_id,
                    name,
                    exc,
                )
        return None

    def _searched_avatar(self, profile: dict[str, Any], draft: PersonaDraft) -> dict[str, Any] | None:
        character_id = str(profile["id"])
        query = " ".join(
            part
            for part in [
                draft.name.strip(),
                draft.identity.strip()[:100],
                "portrait avatar profile picture",
            ]
            if part
        )[:180]
        try:
            result = self.services.avatar_search.search(character_id, query, limit=4)
        except Exception as exc:
            logger.warning("character.onboarding avatar_search_failed character=%s error=%s", character_id, exc)
            return None

        search_id = str(result.get("search_id") or "")
        for candidate in result.get("candidates") or []:
            try:
                metadata = self.services.avatar_search.select(
                    character_id,
                    search_id,
                    str(candidate.get("id") or ""),
                )
                return {
                    "status": "ready",
                    "source": "web_search",
                    "avatar": metadata.model_dump(mode="json"),
                }
            except Exception as exc:
                logger.warning(
                    "character.onboarding avatar_candidate_failed character=%s candidate=%s error=%s",
                    character_id,
                    candidate.get("id"),
                    exc,
                )
        return None

    def _fallback_avatar(self, profile: dict[str, Any], draft: PersonaDraft) -> dict[str, Any]:
        character_id = str(profile["id"])
        metadata = self.services.avatar_store.save_from_bytes(
            character_id,
            _fallback_png(character_id),
            "image/png",
            source="LOCAL_INITIAL",
            title=f"{draft.name} local fallback avatar",
        )
        return {
            "status": "ready",
            "source": "local_fallback",
            "avatar": metadata.model_dump(mode="json"),
        }

    def ensure_avatar(self, profile: dict[str, Any], draft: PersonaDraft) -> dict[str, Any]:
        character_id = str(profile["id"])
        existing = self.services.avatar_store.load(character_id)
        if existing is not None and self.services.avatar_store.asset_path(character_id) is not None:
            return {
                "status": "ready",
                "source": "existing",
                "avatar": existing.model_dump(mode="json"),
            }
        generated = self._generated_avatar(profile, draft)
        if generated is not None:
            return generated
        searched = self._searched_avatar(profile, draft)
        if searched is not None:
            return searched
        return self._fallback_avatar(profile, draft)

    def ensure_voice(self, profile: dict[str, Any]) -> dict[str, Any]:
        persona_dir = Path(profile["persona_path"]).parent
        root = template_root(None)
        try:
            templates = discover_templates(root)
        except Exception as exc:
            logger.warning("character.onboarding voice_templates_failed error=%s", exc)
            return {"status": "unavailable", "reason": str(exc), "template": None}

        if not templates:
            return {
                "status": "unavailable",
                "reason": "no voice templates configured",
                "template": None,
                "fallback_provider": str(getattr(self.settings, "tts_provider", "") or ""),
                "fallback_voice": str(getattr(self.settings, "tts_voice", "") or ""),
            }

        preferred = str(getattr(self.settings, "tts_voice", "") or "").strip()
        selected = preferred if preferred in templates else sorted(templates)[0]
        try:
            write_character_voice(
                persona_dir=persona_dir,
                template=selected,
                voices_root=root,
            )
        except Exception as exc:
            logger.warning(
                "character.onboarding voice_select_failed character=%s template=%s error=%s",
                profile["id"],
                selected,
                exc,
            )
            return {"status": "unavailable", "reason": str(exc), "template": None}
        return {"status": "selected", "template": selected}

    def initialize(
        self,
        profile: dict[str, Any],
        draft: PersonaDraft,
        *,
        creation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        avatar = self.ensure_avatar(profile, draft)
        voice = self.ensure_voice(profile)
        initialization = {
            "avatar": avatar,
            "voice": voice,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        save_creation_metadata(
            profile["persona_path"],
            creation=creation,
            initialization=initialization,
        )
        return initialization
