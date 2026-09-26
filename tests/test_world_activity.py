from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from character_memory.config import Settings
from character_memory.domain.models import WorldObservation
from character_memory.llm.usage import current_llm_usage_context
from character_memory.storage.sqlite import SQLiteStore
from character_memory.time_utils import epoch_us
from character_memory.world_activity import (
    PersonalBrowseAppraisal,
    PersonalBrowsePlan,
    WorldActivityScheduler,
    WorldActivityService,
    WorldPulseCharacterTake,
    WorldPulseDigest,
    WorldPulseRepository,
    WorldPulseTopicDraft,
)


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


class FakeContextBuilder:
    def build(self, character_id, query, *, at, recent_limit):
        return SimpleNamespace(
            persona=f"id: {character_id}\nname: {character_id}",
            mental_state="平静但有点好奇",
            memories=[],
            recent_events=[],
        )


class FakeRuntime:
    def __init__(self, character_id):
        self.persona = f"id: {character_id}\nname: {character_id}"
        self.context_builder = FakeContextBuilder()


class FakeModel:
    def __init__(self):
        self.calls = []
        # The usage scope is a ContextVar, so recording it here is what proves a
        # call site actually set it -- reading the session id back later cannot.
        self.scopes = []

    def structured_for_session(self, prompt, schema, session_id):
        self.calls.append((schema.__name__, session_id, prompt))
        self.scopes.append(current_llm_usage_context())
        if schema is WorldPulseDigest:
            return WorldPulseDigest(
                topics=[
                    WorldPulseTopicDraft(
                        title="Agent Memory 新讨论",
                        summary="多个聚合页面都出现了关于长期 Agent Memory 的新讨论。",
                        category="technology",
                        source_indexes=[1, 2],
                    ),
                    WorldPulseTopicDraft(
                        title="开源工具趋势",
                        summary="开源工具榜单里出现了一批新的开发工具。",
                        category="technology",
                        source_indexes=[2],
                    ),
                ]
            )
        if schema is WorldPulseCharacterTake:
            return WorldPulseCharacterTake(
                interested=True,
                comment="这个方向我会继续看，尤其想知道长期记忆到底怎么避免越积越乱。",
                reason="和人物长期关注的记忆系统有关",
            )
        if schema is PersonalBrowsePlan:
            return PersonalBrowsePlan(
                browse=True,
                query="agent memory architecture",
            )
        if schema is PersonalBrowseAppraisal:
            return PersonalBrowseAppraisal(
                keep=True,
                summary="看到了一篇讨论 Agent Memory 架构的公开文章。",
                personal_note="我对长期记忆如何做治理更感兴趣了。",
            )
        raise AssertionError(f"unexpected schema: {schema}")


class FakeFetcher:
    def fetch_many(self, urls, *, max_chars):
        pages = [
            SimpleNamespace(
                title=f"聚合页 {index}",
                url=url,
                content=f"这是聚合页面 {index} 的热点条目，包含 Agent Memory 与开发工具趋势。",
            )
            for index, url in enumerate(urls, start=1)
        ]
        return pages, []


class FakeObserver:
    def observe(self, query, *, max_pages, max_chars_per_page):
        return {
            "query": query,
            "search_results": 1,
            "errors": [],
            "observations": [
                WorldObservation(
                    title="Agent Memory Architecture",
                    url="https://example.com/agent-memory",
                    source_domain="example.com",
                    snippet="A public article about agent memory.",
                    content="This article discusses long-term memory architecture and governance.",
                )
            ],
        }


def make_access(tmp_path, *, count=3):
    store = SQLiteStore(tmp_path / "world.db")
    settings = Settings(
        api_key="fake",
        db_path=str(tmp_path / "world.db"),
        embedding_provider="deterministic",
        world_pulse_sources=[
            "https://aggregate.example/one",
            "https://aggregate.example/two",
        ],
        world_pulse_commenter_count=count,
        world_pulse_refresh_minutes=60,
        world_pulse_discussion_interval_minutes=360,
        world_browse_interval_minutes=90,
    )
    profiles = [
        {"id": f"c{index:02d}", "name": f"角色{index:02d}"}
        for index in range(count)
    ]
    model = FakeModel()
    runtimes = {profile["id"]: FakeRuntime(profile["id"]) for profile in profiles}
    bundle = SimpleNamespace(model=model, runtimes=runtimes)
    access = SimpleNamespace(
        settings=settings,
        read_store=store,
        character_profiles=lambda: list(profiles),
        require_bundle=lambda: bundle,
        store=lambda: store,
        world_fetcher=FakeFetcher(),
        world_observer=FakeObserver(),
    )
    return store, access, model


def test_world_browse_default_and_dev_summary_are_explicitly_30_minutes():
    assert Settings().world_browse_interval_minutes == 30
    html = Path("src/character_memory/web/dev.html").read_text(encoding="utf-8")
    script = Path("src/character_memory/web/dev.js").read_text(encoding="utf-8")
    assert 'id="worldScheduleSummary"' in html
    assert "renderWorldScheduleSummary" in script

def test_world_pulse_repository_deduplicates_topics_and_character_comments(tmp_path):
    store = SQLiteStore(tmp_path / "pulse.db")
    repo = WorldPulseRepository(store)
    draft = WorldPulseTopicDraft(
        title="同一个热点",
        summary="第一次摘要",
        category="technology",
        source_indexes=[1],
    )

    first = repo.upsert_topic(draft, ["https://example.com/a"], NOW)
    updated = repo.upsert_topic(
        draft.model_copy(update={"summary": "更新后的摘要"}),
        ["https://example.com/b"],
        NOW,
    )
    assert updated["id"] == first["id"]
    assert updated["summary"] == "更新后的摘要"
    assert len(repo.list_topics()) == 1

    comment = repo.add_comment(first["id"], "rin", "我会继续看看。", NOW)
    duplicate = repo.add_comment(first["id"], "rin", "第二次不应该覆盖。", NOW)
    assert duplicate["id"] == comment["id"]
    assert repo.list_comments(first["id"])[0]["content"] == "我会继续看看。"
    assert WorldPulseRepository.MIGRATION in store.list_schema_migrations()
    store.close()


def test_world_pulse_refresh_reads_aggregation_pages_then_summarizes(tmp_path):
    store, access, model = make_access(tmp_path, count=2)
    repo = WorldPulseRepository(store)
    service = WorldActivityService(access, repo)

    result = service.refresh_pulse(now=NOW)

    assert result["refreshed"] is True
    assert len(result["topics"]) == 2
    assert result["topics"][0]["source_urls"] == [
        "https://aggregate.example/one",
        "https://aggregate.example/two",
    ]
    assert any(name == "WorldPulseDigest" for name, _, _ in model.calls)
    stored = repo.list_topics()
    assert {item["title"] for item in stored} == {
        "Agent Memory 新讨论",
        "开源工具趋势",
    }
    store.close()


def test_world_pulse_discussion_is_character_specific_and_persists_recent_fact(tmp_path):
    store, access, _ = make_access(tmp_path, count=2)
    repo = WorldPulseRepository(store)
    service = WorldActivityService(access, repo)
    topic = repo.upsert_topic(
        WorldPulseTopicDraft(
            title="长期记忆讨论",
            summary="聚合站点里有一轮关于长期记忆治理的讨论。",
            category="technology",
            source_indexes=[1],
        ),
        ["https://aggregate.example/one"],
        NOW,
    )

    result = service.discuss_topic(topic["id"], now=NOW)

    assert len(result["outcomes"]) == 2
    assert all(item["interested"] for item in result["outcomes"])
    comments = repo.list_comments(topic["id"])
    assert len(comments) == 2

    for character_id in {"c00", "c01"}:
        events = store.list_events(character_id, limit=10)
        pulse = [event for event in events if event.metadata.get("channel") == "WORLD_PULSE"]
        assert len(pulse) == 1
        assert pulse[0].metadata["topic_id"] == topic["id"]
        assert "公开评论" in pulse[0].content
    store.close()


def test_every_world_activity_model_call_carries_a_world_scope(tmp_path):
    """Attribution is set at the call site, not inferred from the session id.

    A call site that forgets `llm_usage_scope` still reaches the inference
    fallback, but only because the prefix is registered there -- so this asserts
    the explicit scope, which is the real contract.
    """
    store, access, model = make_access(tmp_path, count=1)
    repo = WorldPulseRepository(store)
    service = WorldActivityService(access, repo)

    service.refresh_pulse(now=NOW)
    topic = repo.list_topics()[0]
    service.discuss_topic(topic["id"], now=NOW)
    service.browse_character("c00", now=NOW)

    assert model.scopes
    assert {scope.feature for scope in model.scopes} == {"WORLD"}
    assert {scope.purpose for scope in model.scopes} == {
        "WORLD_PULSE_SUMMARY",
        "WORLD_PULSE_TAKE",
        "WORLD_BROWSE_PLAN",
        "WORLD_BROWSE_APPRAISAL",
    }
    store.close()


def test_personal_browse_is_independent_from_space_posting(tmp_path):
    store, access, model = make_access(tmp_path, count=1)
    repo = WorldPulseRepository(store)
    service = WorldActivityService(access, repo)

    result = service.browse_character("c00", now=NOW)

    assert result["browsed"] is True
    assert result["kept"] is True
    assert result["query"] == "agent memory architecture"
    assert result["source_event_id"] is not None
    events = store.list_events("c00", limit=10)
    assert events[-1].metadata["channel"] == "PERSONAL_BROWSE"
    assert events[-1].event_type.value == "WORLD_OBSERVATION"
    assert any(name == "PersonalBrowsePlan" for name, _, _ in model.calls)
    assert any(name == "PersonalBrowseAppraisal" for name, _, _ in model.calls)

    # Browsing is a World fact only. The World Activity module never creates
    # Character Space posts as a side effect.
    with store._lock:
        table = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='space_posts'"
        ).fetchone()
    assert table is None
    store.close()


def test_world_scheduler_rearms_persisted_browse_when_interval_changes(tmp_path):
    store, access, _ = make_access(tmp_path, count=1)
    access.settings.world_browse_interval_minutes = 90
    repo = WorldPulseRepository(store)
    scheduler = WorldActivityScheduler(access, repo, poll_seconds=10)

    scheduler.run_once(now=NOW)
    before = next(
        item for item in repo.states()
        if item["kind"] == "BROWSE" and item["subject_id"] == "c00"
    )
    assert float(before["configured_interval_minutes"]) == 90

    changed_at = NOW + timedelta(minutes=5)
    access.settings.world_browse_interval_minutes = 30
    restarted = WorldActivityScheduler(access, WorldPulseRepository(store), poll_seconds=10)
    restarted.run_once(now=changed_at)
    after = next(
        item for item in repo.states()
        if item["kind"] == "BROWSE" and item["subject_id"] == "c00"
    )
    assert float(after["configured_interval_minutes"]) == 30
    assert int(after["next_run_at_epoch"]) != int(before["next_run_at_epoch"])
    assert epoch_us(changed_at) < int(after["next_run_at_epoch"]) <= epoch_us(
        changed_at + timedelta(minutes=30)
    )

    preserved = int(after["next_run_at_epoch"])
    same_config = WorldActivityScheduler(access, WorldPulseRepository(store), poll_seconds=10)
    same_config.run_once(now=changed_at)
    final = next(
        item for item in repo.states()
        if item["kind"] == "BROWSE" and item["subject_id"] == "c00"
    )
    assert int(final["next_run_at_epoch"]) == preserved
    store.close()

def test_world_activity_scheduler_keeps_independent_clocks(tmp_path):
    store, access, _ = make_access(tmp_path, count=1)
    repo = WorldPulseRepository(store)
    scheduler = WorldActivityScheduler(access, repo, poll_seconds=10)

    # Pulse is immediately eligible; browse gets its own staggered clock and
    # discussion starts later. None of these values read Space opportunity time.
    outcomes = scheduler.run_once(now=NOW)
    assert [item["kind"] for item in outcomes] == ["PULSE"]

    status = scheduler.status()
    assert status["pulse_refresh_minutes"] == 60
    assert status["discussion_interval_minutes"] == 360
    assert status["browse_interval_minutes"] == 90
    kinds = {(item["kind"], item["subject_id"]) for item in status["states"]}
    assert ("PULSE", "global") in kinds
    assert ("DISCUSS", "global") in kinds
    assert ("BROWSE", "c00") in kinds

    scheduler.force_due("BROWSE", "c00", now=NOW)
    outcomes = scheduler.run_once(now=NOW)
    assert any(item["kind"] == "BROWSE" for item in outcomes)
    store.close()
