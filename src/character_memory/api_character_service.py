from __future__ import annotations

from datetime import datetime
from pathlib import Path
import logging
import shutil
import threading
from typing import Any, Callable

from character_memory.api_contracts import (
    CharacterCapacityConfirmationRequired,
    CharacterCapacityExceeded,
    MAX_ACTIVE_CHARACTERS,
    SOFT_ACTIVE_CHARACTERS,
)
from character_memory.character_onboarding import CharacterOnboardingService
from character_memory.config import (
    discover_character_profiles,
    resolve_persona_path,
    set_character_archived,
    set_character_direct_pending,
    split_archived,
)
from character_memory.group_store import GroupRepository
from character_memory.persona_builder import (
    PersonaDraft,
    normalize_character_id,
    save_persona,
)


logger = logging.getLogger("character_memory.api.characters.service")


class ApiCharacterService:
    """Character registry/capacity mutations owned by the core API.

    Runtime construction stays in api.py because it belongs to the composition
    root. This service owns filesystem/profile mutations and receives one
    callback for registering a newly created profile into the live runtime.
    """

    def __init__(
        self,
        *,
        settings: Any,
        current_bundle: Callable[[], Any | None],
        character_write_lock: threading.RLock,
        register_runtime_character: Callable[[dict[str, Any]], None],
        services: Any | None = None,
        group_store: Any | None = None,
    ):
        self.settings = settings
        self.current_bundle = current_bundle
        self.character_write_lock = character_write_lock
        self.register_runtime_character = register_runtime_character
        # Deletion has to see which conversations still hold the character, or
        # it would remove the persona and leave groups pointing at it. The
        # composition root hands over the store it already owns and this service
        # builds the repository, so api.py does not have to import one -- api.py
        # is under a line budget that exists to keep it a composition root.
        # Optional so the creation-only callers (and their tests) stay as they
        # were; delete_archived refuses rather than guessing when it is absent.
        self.group_store = group_store
        self.onboarding = (
            CharacterOnboardingService(
                settings=settings,
                services=services,
                current_bundle=current_bundle,
            )
            if services is not None
            else None
        )

    def profiles(self) -> list[dict[str, Any]]:
        current = self.current_bundle()
        if current is not None and hasattr(current, "characters"):
            return list(current.characters)
        try:
            return discover_character_profiles(self.settings)
        except (AttributeError, OSError):
            return [
                {
                    "id": "rin",
                    "name": "Rin",
                    "identity": "",
                    "tagline": "",
                    "persona_path": getattr(
                        self.settings,
                        "persona_path",
                        "personas/rin/persona.yaml",
                    ),
                }
            ]

    @staticmethod
    def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in profile.items()
            if key != "persona_path"
        }

    def refresh_cache(self) -> None:
        current = self.current_bundle()
        if current is not None and hasattr(current, "characters"):
            current.characters[:] = discover_character_profiles(self.settings)

    def ensure(self, character_id: str) -> dict[str, Any]:
        from fastapi import HTTPException

        profiles = self.profiles()
        for profile in profiles:
            if profile["id"] == character_id:
                return profile
        known = ", ".join(profile["id"] for profile in profiles)
        raise HTTPException(
            status_code=404,
            detail=f"Unknown character: {character_id}. Known: {known}",
        )

    def set_archived(
        self,
        character_id: str,
        *,
        archived: bool,
        confirm_over_soft_limit: bool = False,
    ) -> dict:
        from fastapi import HTTPException

        with self.character_write_lock:
            try:
                resolve_persona_path(self.settings, character_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            profiles = self.profiles()
            target = next(
                (item for item in profiles if item["id"] == character_id),
                None,
            )
            active_profiles = split_archived(profiles, False)

            if (
                archived
                and target is not None
                and "archived_at" not in target
                and len(active_profiles) <= 1
            ):
                raise HTTPException(
                    status_code=409,
                    detail="至少保留一个未归档人物",
                )

            if (
                not archived
                and target is not None
                and "archived_at" in target
            ):
                active_count = len(active_profiles)
                if active_count >= MAX_ACTIVE_CHARACTERS:
                    raise HTTPException(
                        status_code=409,
                        detail=CharacterCapacityExceeded(
                            active_count,
                            1,
                        ).detail(),
                    )
                if (
                    active_count >= SOFT_ACTIVE_CHARACTERS
                    and not confirm_over_soft_limit
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=CharacterCapacityConfirmationRequired(
                            active_count,
                            1,
                        ).detail(),
                    )

            try:
                stamp = set_character_archived(
                    self.settings,
                    character_id,
                    archived,
                )
            except OSError as exc:
                logger.exception(
                    "api.character archive failed character=%s error=%s",
                    character_id,
                    exc,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"归档失败：{exc}",
                ) from exc

            self.refresh_cache()
            logger.info(
                "api.character archived=%s character=%s",
                archived,
                character_id,
            )

            profile = next(
                (
                    item
                    for item in self.profiles()
                    if item["id"] == character_id
                ),
                None,
            )
            return {
                "ok": True,
                "archived": archived,
                "archived_at": stamp,
                "character": (
                    self.public_profile(profile)
                    if profile
                    else {"id": character_id}
                ),
            }

    def check_capacity(
        self,
        add_count: int = 1,
        *,
        confirm_over_soft_limit: bool = False,
    ) -> dict:
        count = max(0, int(add_count))
        active_count = len(split_archived(self.profiles(), False))
        result_count = active_count + count
        if result_count > MAX_ACTIVE_CHARACTERS:
            raise CharacterCapacityExceeded(active_count, count)
        if (
            count
            and result_count > SOFT_ACTIVE_CHARACTERS
            and not confirm_over_soft_limit
        ):
            raise CharacterCapacityConfirmationRequired(active_count, count)
        return {
            "active_count": active_count,
            "add_count": count,
            "result_count": result_count,
            "soft_limit": SOFT_ACTIVE_CHARACTERS,
            "hard_limit": MAX_ACTIVE_CHARACTERS,
            "warning": result_count > SOFT_ACTIVE_CHARACTERS,
        }

    def _remove_runtime_character(self, character_id: str) -> None:
        current = self.current_bundle()
        if current is None:
            return
        if hasattr(current, "runtimes"):
            current.runtimes.pop(character_id, None)
        runtime_map = getattr(getattr(current, "chat", None), "runtime", None)
        if isinstance(runtime_map, dict):
            runtime_map.pop(character_id, None)

    def _remove_character_artifacts(self, character_id: str, persona_dir: Path) -> None:
        """Delete everything that *defines* a character, and nothing else.

        Shared by the batch-build rollback and the archive-drawer deletion.
        Both need the persona directory (``voice.yaml`` lives inside it), the
        avatar files and the live runtime registration gone. Neither may touch
        the durable record of what the character said or published -- that is
        the relationship's history, not part of the definition, and the archive
        notice promises it survives.
        """
        self._remove_runtime_character(character_id)
        try:
            shutil.rmtree(persona_dir)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception(
                "api.character rollback_persona failed character=%s dir=%s",
                character_id,
                persona_dir,
            )

        avatar_store = getattr(getattr(self.onboarding, "services", None), "avatar_store", None)
        avatar_root = getattr(avatar_store, "root", None)
        if avatar_root is not None:
            try:
                shutil.rmtree(Path(avatar_root) / character_id)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception(
                    "api.character rollback_avatar failed character=%s",
                    character_id,
                )
        self.refresh_cache()

    def create_from_draft(
        self,
        draft: PersonaDraft,
        requested_id: str = "",
        *,
        confirm_over_soft_limit: bool = False,
        skip_capacity_check: bool = False,
        creation: dict[str, Any] | None = None,
    ) -> dict:
        # Keep the registry mutation short. Network-backed avatar generation and
        # search happen after this lock is released, so one slow provider cannot
        # block unrelated archive/create operations.
        with self.character_write_lock:
            if not skip_capacity_check:
                self.check_capacity(
                    1,
                    confirm_over_soft_limit=confirm_over_soft_limit,
                )
            character_id = normalize_character_id(
                draft.name,
                requested_id,
            )
            path = save_persona(
                self.settings.persona_path,
                draft,
                character_id,
            )
            # A batch build introduces a *group*; its members are real
            # characters that simply have no direct chat yet, and dropping five
            # new names into the sidebar buries the ones the user talks to. The
            # write happens before discovery on purpose: the profile registered
            # into the live runtime below has to carry the flag already, or the
            # sidebar would show the character until the next reload.
            if str((creation or {}).get("source") or "") == "ENSEMBLE_BUILDER":
                set_character_direct_pending(self.settings, character_id, True)
            try:
                profiles = discover_character_profiles(self.settings)
                profile = next(
                    profile
                    for profile in profiles
                    if profile["id"] == character_id
                )
                self.register_runtime_character(profile)
            except Exception:
                self._remove_character_artifacts(character_id, path.parent)
                raise

        try:
            initialization = (
                self.onboarding.initialize(
                    profile,
                    draft,
                    creation=creation,
                )
                if self.onboarding is not None
                else None
            )
        except Exception:
            with self.character_write_lock:
                self._remove_character_artifacts(character_id, path.parent)
            raise

        if initialization is not None:
            profile = {**profile, "initialization": initialization}

        logger.info(
            "api.character created character=%s path=%s runtime_loaded=%s",
            character_id,
            path,
            self.current_bundle() is not None,
        )
        return profile

    def rollback_created(self, character_id: str) -> None:
        """Best-effort rollback for a character created inside a batch build."""

        with self.character_write_lock:
            profile = next(
                (
                    item
                    for item in discover_character_profiles(self.settings)
                    if item["id"] == character_id
                ),
                None,
            )
            if profile is None:
                return
            persona_path = Path(profile["persona_path"])
            self._remove_character_artifacts(character_id, persona_path.parent)

    def _group_repository(self) -> Any:
        store = self.group_store
        if store is None:
            raise RuntimeError(
                "delete_archived needs the group store: without it the "
                "deletion cannot see which conversations still hold the "
                "character, and would leave them pointing at a persona that no "
                "longer exists"
            )
        return GroupRepository(store)

    def _groups_holding(self, character_id: str) -> list[Any]:
        """Every conversation that still lists this character, archived or not.

        Archived groups count: an archived group is still a group, and leaving
        the member behind there would strand the same dangling reference.
        """

        repository = self._group_repository()
        return [
            group
            for group in (
                *repository.list_groups(),
                *repository.list_groups(archived=True),
            )
            if character_id in group.member_ids
        ]

    def delete_archived(self, character_id: str, *, confirm_name: str) -> dict[str, Any]:
        """Delete an archived character's definition, keeping its history.

        Three gates, in the order the user can do something about them:

        1. The character must exist.
        2. It must be archived. Archiving is the deliberate first step, and
           requiring it keeps a stale request from deleting someone the user is
           still talking to mid-conversation.
        3. ``confirm_name`` must equal the character's display name exactly.

        Groups are the subtle part. ``remove_member`` refuses to take a group
        below two characters -- a real floor, because a one-person group is not
        a conversation. Rather than bypass it (which would leave a group that
        both ``/v1/groups`` and its archive drawer filter out, i.e. a group the
        user can no longer see *or* restore), deletion refuses and names the
        groups in the way. Archiving those groups first is one click, and it is
        the same step the member picker already asks for.
        """

        from fastapi import HTTPException

        with self.character_write_lock:
            try:
                resolve_persona_path(self.settings, character_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            profile = next(
                (item for item in self.profiles() if item["id"] == character_id),
                None,
            )
            if profile is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown character: {character_id}",
                )
            if "archived_at" not in profile:
                raise HTTPException(
                    status_code=409,
                    detail="只能删除已归档的人物；请先归档，再删除。",
                )

            expected = str(profile.get("name") or character_id)
            if str(confirm_name or "").strip() != expected:
                raise HTTPException(
                    status_code=400,
                    detail=f"名称不一致。请输入「{expected}」以确认删除。",
                )

            groups = self._groups_holding(character_id)
            blocking = [group for group in groups if len(group.member_ids) <= 2]
            if blocking:
                names = "、".join(f"「{group.name}」" for group in blocking)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"TA 还是 {names} 的成员，而该群只有两个人。删掉后这个群会只剩一人，"
                        "无法继续对话。请先归档这个群，再删除人物。"
                    ),
                )

            now = datetime.now().astimezone()
            repository = self._group_repository()
            for group in groups:
                repository.remove_member(group.id, character_id, now)

            persona_dir = Path(profile["persona_path"]).parent
            self._remove_character_artifacts(character_id, persona_dir)

        logger.info(
            "api.character deleted character=%s groups=%s",
            character_id,
            [group.id for group in groups],
        )
        return {
            "deleted": True,
            "character_id": character_id,
            "confirm_name": expected,
            "left_groups": [group.id for group in groups],
        }

    def open_direct(self, character_id: str) -> dict[str, Any]:
        """Give a deferred character its own place in the sidebar.

        Idempotent, because the button that calls it lives on a member list that
        may be a few seconds stale and a second press must not be an error. The
        character was never disabled -- it speaks in its group, in Space and in
        voice the whole time -- so opening changes nothing but the sidebar.
        """

        from fastapi import HTTPException

        with self.character_write_lock:
            try:
                resolve_persona_path(self.settings, character_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            profile = next(
                (item for item in self.profiles() if item["id"] == character_id),
                None,
            )
            if profile is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown character: {character_id}",
                )
            if "archived_at" in profile:
                raise HTTPException(
                    status_code=409,
                    detail="已归档的人物不能直接开启对话；请先恢复，再开始对话。",
                )

            was_pending = "direct_pending" in profile
            set_character_direct_pending(self.settings, character_id, False)
            self.refresh_cache()

        logger.info(
            "api.character direct_opened character=%s was_pending=%s",
            character_id,
            was_pending,
        )
        return {
            "character_id": character_id,
            "direct_pending": False,
            "was_pending": was_pending,
        }
