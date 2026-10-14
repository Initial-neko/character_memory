from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, PersonReaction
from character_memory.visual_runtime import DirectVisualRuntime
from test_p0_19_direct_visual_runtime import _runtime


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def setup_image(tmp_path, *, enabled=True, limit=3, cooldown=8, boot_id="test-boot"):
    service, runtime, provider, avatar, media, store = _runtime(tmp_path)
    service.settings = service.settings.model_copy(update={
        "direct_autonomous_image_generation_enabled": enabled,
        "direct_autonomous_image_daily_max_generations": limit,
        "direct_autonomous_image_cooldown_hours": cooldown,
    })
    compiled = []
    runtime.model = SimpleNamespace(_request=lambda *args, **kwargs: compiled.append(args) or "synthetic character illustration")
    service = DirectVisualRuntime(service.access, clock=lambda: NOW, boot_id=boot_id, settings=service.settings)
    return service, runtime, provider, compiled, store


def source(store, character="mika", *, kind=EventType.TIME_TICK, channel="DIRECT", at=NOW):
    return store.append_event(Event(character_id=character, event_type=kind, event_time=at, content="自然机会", metadata={"channel": channel, "conversation_id": f"{character}:default"}))


ACTION = ActionDecision(type=ActionType.GENERATE_IMAGE, image_purpose="SCENE", visual_intent="合成灯溪镇的安静雨夜氛围")


def test_default_off_rejects_autonomous_before_compiler_provider_and_schema(tmp_path):
    service, runtime, provider, compiled, store = setup_image(tmp_path, enabled=False)
    event = source(store)
    assert service.generate(runtime, event, ACTION) is None
    assert provider.requests == [] and compiled == []
    assert store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='direct_image_generation_jobs'").fetchone() is None
    store.close()


@pytest.mark.parametrize("kind,channel", [(EventType.TIME_TICK,"GROUP"),(EventType.PROACTIVE_INTENT,"SPACE"),(EventType.WORLD_OBSERVATION,"DIRECT"),(EventType.VISUAL_OBSERVATION,"DIRECT")])
def test_wrong_event_and_channel_cannot_buy_generation(tmp_path, kind, channel):
    service, runtime, provider, compiled, store = setup_image(tmp_path)
    assert service.generate(runtime, source(store, kind=kind, channel=channel), ACTION) is None
    assert provider.requests == [] and compiled == []
    store.close()


def test_enabled_generation_is_idempotent_and_marks_synthetic_provenance(tmp_path):
    service, runtime, provider, compiled, store = setup_image(tmp_path)
    event = source(store, kind=EventType.PROACTIVE_INTENT, at=NOW + timedelta(days=30))
    image = service.generate(runtime, event, ACTION, action_index=1)
    assert image.metadata["generated"] and image.metadata["content_scope"] == "SYNTHETIC_CHARACTER_EXPRESSION"
    assert image.metadata["generation_job_id"]
    assert service.generate(runtime, event, ACTION, action_index=1) is None
    assert len(provider.requests) == len(compiled) == 1
    job = dict(store.conn.execute("SELECT * FROM direct_image_generation_jobs").fetchone())
    assert job["status"] == "SUCCEEDED" and job["created_at"] == NOW.isoformat()
    store.close()


def test_daily_application_limit_and_character_cooldown_before_any_paid_call(tmp_path):
    service, runtime, provider, compiled, store = setup_image(tmp_path, limit=1)
    assert service.generate(runtime, source(store), ACTION)
    assert service.generate(runtime, source(store, "another", at=NOW + timedelta(days=99)), ACTION) is None
    assert len(provider.requests) == len(compiled) == 1
    store.close()
    service, runtime, provider, compiled, store = setup_image(tmp_path / "cooldown")
    assert service.generate(runtime, source(store), ACTION)
    assert service.generate(runtime, source(store, at=NOW + timedelta(days=99)), ACTION) is None
    assert len(provider.requests) == len(compiled) == 1
    store.close()


def test_failure_preserves_text_consumes_reservation_and_restart_never_retries(tmp_path):
    service, runtime, provider, compiled, store = setup_image(tmp_path)
    original = store.append_event(Event(character_id="mika", event_type=EventType.CHARACTER_MESSAGE, event_time=NOW, content="合法文字先提交"))
    event = source(store)
    provider.generate = lambda request: (_ for _ in ()).throw(RuntimeError("provider failed"))
    with pytest.raises(RuntimeError, match="provider failed"):
        service.generate(runtime, event, ACTION)
    assert store.get_event(original.id).content == "合法文字先提交"
    assert dict(store.conn.execute("SELECT * FROM direct_image_generation_jobs").fetchone())["status"] == "FAILED"
    restarted = DirectVisualRuntime(service.access, settings=service.settings, clock=lambda: NOW, boot_id="new-boot")
    assert restarted.generate(runtime, event, ACTION) is None
    assert len(compiled) == 1
    assert restarted.generate(runtime, source(store), ACTION) is None
    assert len(compiled) == 1
    store.close()


def test_atomic_reservation_prevents_duplicate_and_parallel_provider_work(tmp_path):
    from character_memory.image_generation_store import ImageGenerationJobs
    from character_memory.storage.sqlite import SQLiteStore
    path = tmp_path / "jobs.db"
    first, second = SQLiteStore(path), SQLiteStore(path)
    jobs1, jobs2 = ImageGenerationJobs(first, boot_id="same"), ImageGenerationJobs(second, boot_id="same")
    def reserve(jobs):
        return jobs.reserve(character_id="a", source_event_id=1, action_index=0, request="same", now=NOW, daily_max=3, cooldown_hours=8)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, [jobs1, jobs2]))
    assert sum(result[0] is not None for result in results) == 1
    assert first.conn.execute("SELECT COUNT(*) FROM direct_image_generation_jobs").fetchone()[0] == 1
    jobs1.finish(next(result[0] for result in results if result[0]), status="SUCCEEDED")
    assert jobs2.reserve(character_id="a", source_event_id=2, action_index=0, request="new", now=NOW, daily_max=3, cooldown_hours=8)[1] == "COOLDOWN"
    first.close(); second.close()


def test_formal_settings_and_context(tmp_path):
    from character_memory.config import Settings
    from character_memory.runtime.context import compile_context
    assert Settings().direct_autonomous_image_generation_enabled is False
    assert Settings().direct_autonomous_image_daily_max_generations == 3
    assert Settings().direct_autonomous_image_cooldown_hours == 8
    service, runtime, provider, compiled, store = setup_image(tmp_path)
    assert "image_purpose" in compile_context("persona", "", [], source(store), allow_generate_image=True)
    assert "image_purpose" not in compile_context("persona", "", [], source(store, channel="GROUP"), allow_generate_image=True)
    store.close()


def test_sanitization_uses_event_policy(tmp_path, monkeypatch):
    from character_memory.runtime.person_runtime import PersonRuntime
    from character_memory import visual_runtime
    service, runtime, provider, compiled, store = setup_image(tmp_path)
    monkeypatch.setattr(visual_runtime, "_service", service)
    reaction = PersonReaction(actions=[ACTION])
    minimal = SimpleNamespace(store=store)
    accepted, _, decisions = PersonRuntime._sanitize_resource_actions(minimal, reaction, event=source(store))
    assert accepted.actions == [ACTION] and decisions[0]["decision"] == "ALLOW_GENERATION"
    denied, _, decisions = PersonRuntime._sanitize_resource_actions(minimal, reaction, event=source(store, channel="GROUP"))
    assert denied.actions == [] and decisions[0]["reason"] == "CHANNEL_OR_EVENT_DENIED"
    store.close()


def test_utc_midnight_and_restart_unknown(tmp_path):
    from character_memory.image_generation_store import ImageGenerationJobs
    from character_memory.storage.sqlite import SQLiteStore
    store = SQLiteStore(tmp_path / "jobs.db")
    jobs = ImageGenerationJobs(store, boot_id="old")
    local = datetime(2026, 10, 15, 1, tzinfo=timezone(timedelta(hours=8)))
    job, _ = jobs.reserve(character_id="a", source_event_id=1, action_index=0, request="one", now=local, daily_max=1, cooldown_hours=0)
    jobs = ImageGenerationJobs(store, boot_id="new")
    assert store.conn.execute("SELECT status FROM direct_image_generation_jobs WHERE id=?", [job]).fetchone()[0] == "INTERRUPTED"
    assert jobs.reserve(character_id="b", source_event_id=2, action_index=0, request="two", now=datetime(2026,10,14,23,tzinfo=timezone.utc), daily_max=1, cooldown_hours=0)[1] == "DAILY_LIMIT"
    assert jobs.reserve(character_id="b", source_event_id=3, action_index=0, request="three", now=datetime(2026,10,15,tzinfo=timezone.utc), daily_max=1, cooldown_hours=0)[0]
    store.close()


def test_global_pending_and_conflicting_request_reservations(tmp_path):
    from character_memory.image_generation_store import ImageGenerationJobs
    from character_memory.storage.sqlite import SQLiteStore
    store = SQLiteStore(tmp_path / "jobs.db")
    jobs = ImageGenerationJobs(store, boot_id="same")
    job, _ = jobs.reserve(character_id="a", source_event_id=1, action_index=0, request="one", now=NOW, daily_max=3, cooldown_hours=0)
    assert jobs.reserve(character_id="b", source_event_id=2, action_index=0, request="two", now=NOW, daily_max=3, cooldown_hours=0)[1] == "BUSY"
    assert jobs.reserve(character_id="a", source_event_id=1, action_index=0, request="changed", now=NOW, daily_max=3, cooldown_hours=0)[1] == "REQUEST_CONFLICT"
    jobs.finish(job, status="FAILED")
    assert jobs.reserve(character_id="b", source_event_id=2, action_index=0, request="two", now=NOW, daily_max=3, cooldown_hours=0)[0]
    store.close()


def test_superseded_result_consumes_quota_without_visible_image(tmp_path):
    service, runtime, provider, compiled, store = setup_image(tmp_path, limit=1)
    checks = iter([True, False])
    assert service.generate(runtime, source(store), ACTION, still_current=lambda: next(checks)) is None
    assert store.conn.execute("SELECT status FROM direct_image_generation_jobs").fetchone()[0] == "SUPERSEDED"
    assert not [event for event in store.list_events("mika", limit=20) if event.metadata.get("generated")]
    assert service.generate(runtime, source(store, "other"), ACTION) is None
    assert len(compiled) == len(provider.requests) == 1
    store.close()


def test_lazy_schema_migration_is_recorded_once_and_reopens(tmp_path):
    from character_memory.image_generation_store import ImageGenerationJobs
    from character_memory.storage.sqlite import SQLiteStore
    path = tmp_path / "jobs.db"
    store = SQLiteStore(path)
    ImageGenerationJobs(store, boot_id="one")
    assert store.conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='visual/001-direct-image-generation-jobs'").fetchone()[0] == 1
    store.close()
    store = SQLiteStore(path)
    ImageGenerationJobs(store, boot_id="two")
    assert store.conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='visual/001-direct-image-generation-jobs'").fetchone()[0] == 1
    store.close()



@pytest.mark.parametrize("entry", ["wake", "intent"])
@pytest.mark.parametrize("enabled,fail", [(False,False),(True,False),(True,True)])
def test_formal_dispatch_through_person_and_async_worker(tmp_path, monkeypatch, entry, enabled, fail):
    from character_memory.application.chat_service import ChatService
    from character_memory.application.clock import FixedClock
    from character_memory.llm.client import PersonModel
    from character_memory.memory.embedding import DeterministicEmbedding
    from character_memory.memory.recall import VectorRecall
    from character_memory.runtime.person_runtime import PersonRuntime
    from character_memory import visual_runtime
    service, unused, provider, compiled, store = setup_image(tmp_path, enabled=enabled)
    class Model(PersonModel):
        def __init__(self):
            self.contexts = []
        def react(self, context):
            self.contexts.append(context)
            return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="Here is a quiet moment."), ACTION])
        def plan_day(self, context):
            raise AssertionError("unexpected day planner")
        def write_diary(self, context):
            raise AssertionError("unexpected diary")
        def _request(self, *args, **kwargs):
            compiled.append(args)
            return "synthetic scene"
    model = Model()
    embeddings = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "persona")
    chat = ChatService(store, {"mika": runtime}, FixedClock(NOW))
    threads = []
    submit = service.submit
    def capture(*args, **kwargs):
        thread = submit(*args, **kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(service, "submit", capture)
    monkeypatch.setattr(visual_runtime, "_service", service)
    if fail:
        def failure(request):
            provider.requests.append(request)
            raise RuntimeError("isolated provider failure")
        monkeypatch.setattr(provider, "generate", failure)
    if entry == "wake":
        result = chat.dispatch_wake(character_id="mika", at=NOW, conversation_id="image-chat")
    else:
        result = chat.dispatch_proactive_intent(character_id="mika", intent_id=7, content="share a scene", at=NOW)
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    messages = store.list_events("mika", limit=20, event_type=EventType.CHARACTER_MESSAGE.value)
    assert any(event.content == "Here is a quiet moment." for event in messages)
    images = [event for event in messages if event.metadata.get("generated")]
    assert len(images) == int(enabled and not fail)
    assert len(provider.requests) == len(compiled) == int(enabled)
    assert ("image_purpose" in model.contexts[0]) == enabled
    if enabled:
        job = dict(store.conn.execute("SELECT * FROM direct_image_generation_jobs").fetchone())
        assert job["status"] == ("FAILED" if fail else "SUCCEEDED")
        assert job["source_event_id"] == result.event.id and job["action_index"] == 1
        if images:
            assert images[0].metadata["source_event_id"] == result.event.id
    store.close()
