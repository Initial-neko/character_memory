"""Publish gate uses real media persistence with offline provider doubles."""
from datetime import datetime, timezone
from types import SimpleNamespace

from test_space_autonomy import _access
from test_space_media_executor import FakeImageProvider, FakeVisualModel
from character_memory.media import MediaStorage
from character_memory.space_autonomy import SpaceAutonomyService
from character_memory.space_store import SpaceRepository
from character_memory.domain.models import SpacePostPlan, SpaceMediaIntent
from character_memory.space_autonomy import SpaceAutonomyScheduler
import pytest


@pytest.mark.parametrize("kind", ["VOICE", "GENERATE_VIDEO"])
def test_required_image_reserves_non_image_slots_before_any_provider_call(kind):
    from character_memory.space_media_executor import materialize_space_post_media

    class NeverExecute:
        def execute(self, *args, **kwargs):
            raise AssertionError("insufficient attachment slots must not buy media")

    intent = SpaceMediaIntent(type=kind, voice_text="hello" if kind == "VOICE" else None,
                             video_prompt="a room" if kind == "GENERATE_VIDEO" else None)
    result = materialize_space_post_media(NeverExecute(), "c00", [intent], content="hello",
        now=datetime.now(timezone.utc), max_items=1)
    assert result["reason"] == "IMAGE_REQUIRED"
    assert result["relations"] == []
    assert result["errors"][0]["type"] == "MEDIA_LIMIT"


def test_shared_media_builder_generates_image_first_and_preserves_voice():
    from character_memory.space_media_executor import materialize_space_post_media

    class Executor:
        calls = []

        def execute(self, character_id, intents, **kwargs):
            intent = intents[0]
            self.calls.append((intent.type.value, intent.count))
            kind = "VOICE" if intent.type.value == "VOICE" else "IMAGE"
            return {"relations": [{"media_id": kind, "media_type": kind}], "errors": []}

        def discard(self, relations):
            raise AssertionError("all purchased assets must remain in the post")

    executor = Executor()
    result = materialize_space_post_media(executor, "c00", [SpaceMediaIntent(type="VOICE", voice_text="hello")],
        content="", now=datetime.now(timezone.utc), max_items=2)
    assert executor.calls == [("GENERATE_IMAGE", 1), ("VOICE", 1)]
    assert [item["media_type"] for item in result["relations"]] == ["IMAGE", "VOICE"]


def test_shared_media_builder_cleans_image_if_archived_before_voice():
    from character_memory.space_media_executor import materialize_space_post_media

    class Executor:
        discarded = []

        def execute(self, *args, **kwargs):
            return {"relations": [{"media_id": "image", "media_type": "IMAGE"}], "errors": []}

        def discard(self, relations):
            self.discarded.extend(relations)

    checks = []

    def active():
        checks.append(True)
        if len(checks) == 2:
            raise ValueError("archived")

    executor = Executor()
    with pytest.raises(ValueError, match="archived"):
        materialize_space_post_media(executor, "c00", [SpaceMediaIntent(type="VOICE", voice_text="hello")],
            content="", now=datetime.now(timezone.utc), max_items=2, before_execute=active)
    assert executor.discarded == [{"media_id": "image", "media_type": "IMAGE"}]


@pytest.mark.parametrize("kind", ["VOICE", "GENERATE_VIDEO"])
def test_dev_media_cannot_publish_without_required_image(tmp_path, monkeypatch, kind):
    from fastapi.testclient import TestClient
    from test_character_space import _config, _save_test_audio
    from character_memory.api import create_api
    from character_memory.space_web import attach_space_routes

    monkeypatch.setenv("CHARACTER_MEMORY_EAGER_WARMUP", "0")
    app = create_api(str(_config(tmp_path, count=2)))
    attach_space_routes(app)
    with TestClient(app) as client:
        access = app.state.character_memory
        asset = _save_test_audio(app, "c00", 0)
        calls = []

        def execute(character_id, intents, **kwargs):
            calls.extend(item.type.value for item in intents)
            if intents[0].type.value == "GENERATE_IMAGE":
                return {"relations": [], "errors": [{"type": "GENERATE_IMAGE", "error": "quota exhausted"}]}
            return {"relations": [{"media_id": asset.id, "media_type": "VOICE",
                "source_type": "GENERATED", "metadata": {}}], "errors": []}

        access.space_autonomy.media_executor.execute = execute
        response = client.post("/v1/space/dev/media/c00", json={
            "type": kind, "content": "hello", "voice_text": "hello",
            "video_prompt": "a room",
        })
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "IMAGE_REQUIRED"
        assert calls == ["GENERATE_IMAGE"]
        assert access.space_repository.list_posts(limit=10)[0] == []


def _service(tmp_path, *, provider=None):
    access, store, model = _access(tmp_path, ids=("c00",))
    model._request = FakeVisualModel()._request
    access.media_storage = MediaStorage(tmp_path / "media")
    access.services = SimpleNamespace(image_generation_providers={"fake": provider} if provider else {})
    access.settings.image_generation_provider = "fake"
    return SpaceAutonomyService(access, SpaceRepository(store)), store


def test_text_plan_gets_real_generated_image_before_publication(tmp_path):
    service, store = _service(tmp_path, provider=FakeImageProvider())
    result = service.run_opportunity("c00", now=datetime.now(timezone.utc), cascade=False)
    assert result["posted"] is True
    assert result["post"]["media_items"][0]["media_type"] == "IMAGE"
    asset = store.get_media_asset(result["post"]["media_items"][0]["media_id"])
    assert asset is not None and asset.mime_type == "image/png"
    assert result["post"]["media_items"][0]["metadata"]["visual_intent"].endswith("今天想安静一点，晚点再做别的。")
    store.close()


def test_unavailable_generator_cannot_publish_text_or_social_event(tmp_path):
    service, store = _service(tmp_path)
    result = service.run_opportunity("c00", now=datetime.now(timezone.utc), cascade=False)
    assert result["posted"] is False
    assert result["reason"] == "IMAGE_REQUIRED"
    assert result["media_errors"]
    assert service.repository.list_posts(limit=10)[0] == []
    assert not [event for event in store.list_events("c00") if event.event_type.value == "SOCIAL_POST"]
    store.close()


def test_media_disabled_blocks_publication_without_calling_provider(tmp_path):
    class NeverCalled(FakeImageProvider):
        def generate(self, request):
            raise AssertionError("disabled media must not buy a generation")

    service, store = _service(tmp_path, provider=NeverCalled())
    service.access.settings.space_media_enabled = False
    result = service.run_opportunity("c00", now=datetime.now(timezone.utc), cascade=False)
    assert result["posted"] is False
    assert result["reason"] == "IMAGE_REQUIRED"
    store.close()


def test_failed_explicit_generation_is_not_retried_or_published(tmp_path):
    class Failing(FakeImageProvider):
        calls = 0

        def generate(self, request):
            self.calls += 1
            raise RuntimeError("quota exhausted")

    provider = Failing()
    service, store = _service(tmp_path, provider=provider)
    model = service.access.require_bundle().model
    model.structured_for_session = lambda *args: SpacePostPlan(
        social_post="雨后的窗边。", media_intents=[SpaceMediaIntent(
            type="GENERATE_IMAGE", purpose="SCENE", visual_intent="雨后窗边",
        )],
    )
    result = service.run_opportunity("c00", now=datetime.now(timezone.utc), cascade=False)
    assert result["reason"] == "IMAGE_REQUIRED"
    assert provider.calls == 1
    assert "quota exhausted" in result["media_errors"][0]["error"]
    store.close()


def test_silence_does_not_generate_or_publish(tmp_path):
    class Counting(FakeImageProvider):
        calls = 0

        def generate(self, request):
            self.calls += 1
            return super().generate(request)

    provider = Counting()
    service, store = _service(tmp_path, provider=provider)
    service.access.require_bundle().model.structured_for_session = lambda *args: SpacePostPlan()
    result = service.run_opportunity("c00", now=datetime.now(timezone.utc), cascade=False)
    assert result["posted"] is False
    assert provider.calls == 0
    assert not result["media_errors"]
    store.close()


def test_scheduler_records_missing_image_as_failure_with_reason(tmp_path):
    from datetime import timedelta

    service, store = _service(tmp_path)
    service.access.settings.space_opportunity_interval_minutes = 30
    scheduler = SpaceAutonomyScheduler(service.access, service.repository, poll_seconds=10)
    start = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
    scheduler.status(start)
    result = scheduler.run_once(start + timedelta(minutes=31))[0]
    run = service.repository.get_opportunity_run(result["run_id"])
    assert run["status"] == "FAILED"
    assert run["error"] == "IMAGE_REQUIRED"
    assert run["details"]["reason"] == "IMAGE_REQUIRED"
    assert run["details"]["media_errors"]
    store.close()
