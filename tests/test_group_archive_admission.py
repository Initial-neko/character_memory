"""Group acceptance must recheck archive state at the durable write boundary."""
import base64
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.application.async_conversation import group_channel
from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.async_web import attach_async_routes
from character_memory.group_store import GroupRepository
from character_memory.group_web import attach_group_routes
from character_memory.storage.sqlite import SQLiteStore
from character_memory.visual_capture_web import attach_visual_capture_routes


@pytest.mark.parametrize("kind", ["text", "image", "visual"])
def test_archive_between_validation_and_write_rejects_user_fact(tmp_path, monkeypatch, kind):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.yaml"
    media_dir = tmp_path / "media"
    config.write_text("\n".join([
        'api_key: ""', 'embedding_provider: "deterministic"',
        f'db_path: "{(tmp_path / "archive-race.db").as_posix()}"',
        f'media_dir: "{media_dir.as_posix()}"',
        f'persona_path: "{(root / "personas/rin/persona.yaml").as_posix()}"',
    ]), encoding="utf-8")
    app = create_api(str(config))
    attach_group_routes(app)
    attach_async_routes(app)
    attach_visual_capture_routes(app)
    access = app.state.character_memory
    access.reaction_scheduler.quiet_seconds = 60
    access.reaction_scheduler.max_burst_seconds = 60
    repo = GroupRepository(access.store())
    members = [p["id"] for p in access.character_profiles()][:2]
    now = datetime.now(timezone.utc)
    group = repo.create_group("Archive boundary", members, now)
    module = import_module("character_memory.visual_capture_web" if kind == "visual" else "character_memory.async_web")
    original = module.build_group_user_event

    def archive_before_fact(*args, **kwargs):
        # Deterministically place the real archive write after route validation
        # (and any durable image upload), before accepting the user Event.
        repo.archive_group(group.id, now)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "build_group_user_event", archive_before_fact)
    image = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xfftest-image").decode()
    payload = {"message": "must not land after archive"}
    url = f"/v1/groups/{group.id}/messages"
    if kind == "image":
        payload["image"] = {"filename": "photo.jpg", "data_url": image}
    elif kind == "visual":
        payload["visual_frames"] = [{"source": "DISPLAY", "data_url": image}]
        url = f"/v1/visual/groups/{group.id}/messages"

    with TestClient(app) as client:
        response = client.post(url, json=payload)
        assert response.status_code == 404, response.text
        assert repo.get_group(group.id) is None
        assert repo.list_events(group.id) == []
        assert group_channel(group.id) not in access.reaction_scheduler._states
        assert access.store().conn.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
        assert not any(p.is_file() for p in media_dir.rglob("*"))
        assert client.get(f"/v1/groups/{group.id}/history").status_code == 200
        monkeypatch.setattr(module, "build_group_user_event", original)
        assert client.post(f"/v1/groups/{group.id}/restore").status_code == 200
        accepted = client.post(url, json=payload)
        assert accepted.status_code == 202, accepted.text
        assert len(repo.list_events(group.id)) == 1
        assert group_channel(group.id) in access.reaction_scheduler._states


def test_application_persistence_rechecks_archive_after_build(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    store = SQLiteStore(tmp_path / "application-race.db")
    service = GroupConversationService(store, {}, FixedClock(now))
    group = service.repo.create_group("Application boundary", ["rin", "momo"], now)
    module = import_module("character_memory.application.group_conversation_service")
    original = module.build_group_user_event

    def archive_before_fact(*args, **kwargs):
        service.repo.archive_group(group.id, now)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "build_group_user_event", archive_before_fact)
    with pytest.raises(KeyError):
        service.persist_user_event(group.id, "rejected after archive")
    assert service.repo.list_events(group.id) == []
    monkeypatch.setattr(module, "build_group_user_event", original)
    service.repo.restore_group(group.id)
    accepted = service.persist_user_event(group.id, "accepted after restore")
    assert accepted.id is not None
    assert [event.id for event in service.repo.list_events(group.id)] == [accepted.id]
    store.close()
