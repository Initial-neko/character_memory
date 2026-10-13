import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.ensemble_builder import EnsembleBuilderService, EnsembleRepository
from character_memory.ensemble_web import attach_ensemble_routes
from character_memory.storage.sqlite import SQLiteStore
from character_memory.api_character_service import ApiCharacterService
from character_memory.config import Settings, discover_character_profiles
from test_ensemble_builder import _access, FakeEnsembleModel


SCENE = {
    "background": "雨后的异世界小镇旅店，氛围温暖",
    "characters": "三位成年朋友，各有自己的关注和表达",
    "relationships": "彼此熟悉，对远来的旅人抱有好感",
    "opening": "旅人走进门，可以闲聊或安静休息",
}


def _original_model(access):
    model = access.require_bundle().model
    def research(prompt, schema, session_id):
        assert "原创" in prompt
        return FakeEnsembleModel().structured_for_session("[PUBLIC SOURCE]", schema, session_id)
    model.structured_for_session = research
    return model


def test_scene_prepare_persists_fresh_characters_and_input_on_restart(tmp_path):
    access, store, observer, profiles, created = _access(tmp_path)
    _original_model(access)
    service = EnsembleBuilderService(access, EnsembleRepository(store))
    build = service.prepare_scene(SCENE)
    assert build["mode"] == "SCENE"
    assert build["scene"] == SCENE
    assert all(item["existing_character_id"] is None for item in build["drafts"])
    assert created == []
    assert observer.calls == []
    store.close()
    store = SQLiteStore(str(tmp_path / "ensemble.db"))
    access.read_store = store
    access.store = lambda: store
    service = EnsembleBuilderService(access, EnsembleRepository(store))
    restored = service.payload(service.repository.get(build["group_id"]))
    assert restored["scene"] == SCENE
    assert restored["drafts"][0]["existing_character_id"] is None
    # A per-member model retry must retain the fresh-instance decision too.
    retried = service.retry_member(build["group_id"], 0)
    assert retried["drafts"][0]["existing_character_id"] is None
    activated = service.confirm(build["group_id"], [0, 1, 2])
    assert len(created) == 3
    assert all("原创情景" in row["tags"] for row in access.creation_records)


def test_scene_route_validates_inputs_before_creating_build(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    _original_model(access)
    app = FastAPI()
    app.state.character_memory = access
    attach_ensemble_routes(app)
    with TestClient(app) as client:
        invalid = client.post("/v1/ensembles/scenes/prepare", json={**SCENE, "background": " "})
        assert invalid.status_code == 422
        assert store.conn.execute("SELECT count(*) FROM ensemble_builds").fetchone()[0] == 0
        response = client.post("/v1/ensembles/scenes/prepare", json=SCENE)
        assert response.status_code == 200
        assert response.json()["build"]["scene"] == SCENE


def test_scene_generation_failure_keeps_input_for_retry(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    def unavailable():
        raise RuntimeError("fixture provider unavailable")
    access.require_bundle = unavailable
    app = FastAPI()
    app.state.character_memory = access
    attach_ensemble_routes(app)
    with TestClient(app) as client:
        response = client.post("/v1/ensembles/scenes/prepare", json=SCENE)
        assert response.status_code == 200
        build = response.json()["build"]
        assert build["status"] == "FAILED"
        assert json.loads(build["prompt"]) == SCENE
        resumed = client.get("/v1/ensembles").json()["build"]
        assert resumed["scene"] == SCENE


def test_scene_confirmation_leaves_existing_same_name_persona_untouched(tmp_path):
    access, store, _, profiles, _ = _access(tmp_path)
    _original_model(access)
    existing = tmp_path / "personas" / "kurisu" / "persona.yaml"
    existing.parent.mkdir(parents=True)
    original = "id: kurisu\nname: Kurisu\nidentity: Existing researcher\n"
    existing.write_text(original, encoding="utf-8")
    settings = Settings(persona_path=str(existing), db_path=str(tmp_path / "ensemble.db"), embedding_provider="deterministic")
    creator = ApiCharacterService(
        settings=settings, current_bundle=lambda: None,
        character_write_lock=threading.RLock(),
        register_runtime_character=lambda profile: profiles.append(profile),
    )
    access.character_profiles = lambda: discover_character_profiles(settings)
    access.create_character_from_draft = creator.create_from_draft
    access.rollback_created_character = creator.rollback_created
    service = EnsembleBuilderService(access, EnsembleRepository(store))
    build = service.prepare_scene(SCENE)
    active = service.confirm(build["group_id"], [0, 1, 2])
    assert existing.read_text(encoding="utf-8") == original
    assert "kurisu" not in active["group"]["member_ids"]
    assert len(active["created_character_ids"]) == 3
    assert all(character_id.startswith("character-") for character_id in active["created_character_ids"])
