"""Voice-line endpoints for the chat page's per-character drawer.

These live on the app that serves the chat page, not on the TTS Lab. The front
end reaches them through ``CM.api``, which is a *relative* fetch and therefore
same-origin; ``:9002`` is a different process and a different origin. The shape
follows ``avatar_web.py``: an ``attach_*_routes(app)`` function over
``app.state.character_memory``.

The logic lives in plain functions taking explicit arguments, so it can be
tested behaviourally over ``tmp_path`` instead of through a full ``create_api``.
The routes are a thin shell over them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import BaseModel, ConfigDict

from character_memory.voices import (
    CHARACTER_VOICE_FILENAME,
    VoiceProfileError,
    load_character_voice,
    load_template,
)


logger = logging.getLogger("character_memory.voice_web")


class CharacterVoiceRequest(BaseModel):
    """The drawer's write: a template name, or ``None`` to clear the reference.

    Declared at module scope on purpose. The module uses ``from __future__
    import annotations``, so FastAPI resolves the annotation by evaluating the
    string against the *module* globals -- a model defined inside
    ``attach_voice_routes`` is a local there and cannot be resolved, which
    silently demotes the body to a query parameter and answers 422 to every
    call.
    """

    model_config = ConfigDict(extra="forbid")

    template: str | None = None


def voice_snapshot(*, characters: Iterable[dict], voices_root: Path) -> dict:
    """Return the template list and every character's voice status.

    ``characters`` is what ``discover_character_profiles`` returns; taking it as
    an argument rather than globbing a root keeps "who is a character" defined
    in exactly one place.

    ``templates`` carries ``used_by`` because only the server can count global
    references -- the browser sees just its own character list, and without the
    count an overwrite silently changes someone else's voice.

    A template whose file is unusable is reported in ``errors`` rather than
    raised: one bad template must not blank the whole drawer, and this list is
    only a picker -- the sidecar's own loader has the final say.
    """

    templates: list[dict] = []
    errors: list[str] = []
    paths = sorted(voices_root.glob("*.yaml")) if voices_root.exists() else []
    for path in paths:
        try:
            profile = load_template(path)
        except VoiceProfileError as exc:
            errors.append(f"{path.stem}: {exc}")
            continue
        raw: dict = {}
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, yaml.YAMLError):
            pass
        templates.append(
            {
                "name": profile.voice_id,
                "ref_text": profile.ref_text,
                # Provenance, present only on frozen templates. GSV never reads
                # these; the drawer shows them so two voices can be told apart.
                "created_at": raw.get("created_at"),
                "instruct": raw.get("instruct"),
                "used_by": [],
            }
        )

    by_name = {entry["name"]: entry for entry in templates}
    status_by_character: dict[str, dict] = {}
    for item in characters:
        if "archived_at" in item:
            continue
        character_id = item["id"]
        persona_path = Path(item["persona_path"])
        try:
            referenced = load_character_voice(persona_path)
            error = None
        except VoiceProfileError as exc:
            # Configured but broken. Reported loudly rather than degraded,
            # because a wrong voice is harder to notice than no voice.
            referenced, error = None, str(exc)
        if error:
            status = "error"
        elif referenced is None:
            status = "unset"
        elif referenced in by_name:
            status = "set"
            by_name[referenced]["used_by"].append(character_id)
        else:
            status = "missing"
            error = f"references unknown template {referenced!r}"
        status_by_character[character_id] = {
            "template": referenced,
            "status": status,
            "error": error,
        }

    return {"templates": templates, "characters": status_by_character, "errors": errors}


def write_character_voice(*, persona_dir: Path, template: str | None, voices_root: Path) -> None:
    """Point a character at a template, or clear the reference.

    ``None`` removes the file so the character falls back to the default
    template -- a different intent from naming one, so it is a distinct value
    rather than an empty string.

    Atomic, following ``persona_builder.save_persona``: a half-written reference
    would be read by the next reload as a corrupt file.
    """

    reference = persona_dir / CHARACTER_VOICE_FILENAME
    if template is None:
        try:
            reference.unlink()
        except FileNotFoundError:
            pass
        return

    name = str(template).strip()
    if not (voices_root / f"{name}.yaml").is_file():
        raise ValueError(f"Unknown template: {name}")

    temp = reference.with_suffix(".yaml.tmp")
    temp.write_text(yaml.safe_dump({"template": name}, sort_keys=False), encoding="utf-8")
    temp.replace(reference)


def attach_voice_routes(app) -> None:
    """Attach the drawer's voice endpoints."""

    from fastapi import HTTPException

    from character_memory.config import discover_character_profiles, split_archived
    from character_memory.tts_lab import GsvVoiceReloader
    from character_memory.voices import template_root

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError(
            "create_api() must expose app.state.character_memory before voice routes are attached"
        )

    settings = access.settings
    reloader = GsvVoiceReloader()

    def _profile(character_id: str) -> dict:
        """Resolve through the discovered whitelist; never join the raw id."""

        profile = next(
            (item for item in discover_character_profiles(settings) if item["id"] == character_id),
            None,
        )
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")
        return profile

    def _persona_dir(character_id: str) -> Path:
        return Path(_profile(character_id)["persona_path"]).parent

    def _reload() -> tuple[bool, str | None]:
        """Non-fatal: the file is already on disk and GSV picks it up at next start.

        Deliberately local rather than reusing ``tts_lab._reload_voices``: that
        one takes the reloader as an argument and belongs to the Lab app, while
        this app owns its own ``GsvVoiceReloader``. Reaching across for six lines
        would couple this module to a neighbour's writer to save less than it costs.
        """

        try:
            reloader.reload()
            return True, None
        except Exception as exc:
            logger.warning("GSV voice reload failed: %s", exc)
            return False, str(exc) or exc.__class__.__name__

    @app.get("/v1/voice-templates")
    def voice_templates():
        return voice_snapshot(
            characters=split_archived(discover_character_profiles(settings), False),
            voices_root=template_root(None),
        )

    @app.post("/v1/characters/{character_id}/voice")
    def set_character_voice(character_id: str, req: CharacterVoiceRequest):
        profile = _profile(character_id)
        if "archived_at" in profile:
            raise HTTPException(status_code=409, detail="archived characters do not participate in active voice synthesis")
        persona_dir = _persona_dir(character_id)
        try:
            write_character_voice(
                persona_dir=persona_dir,
                template=req.template,
                voices_root=template_root(None),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        activated, reason = _reload()
        return {
            "ok": True,
            "character_id": character_id,
            "template": req.template,
            "activated": activated,
            "reason": reason,
        }
