from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    Event,
    EventType,
    PersonReaction,
    SpacePostPlan,
    SpaceMediaIntent,
)
from character_memory.llm.client import ModelCallResult, ModelCallTrace, PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.space_autonomy import SpaceAutonomyScheduler, SpaceAutonomyService
from character_memory.space_store import SpaceRepository
from character_memory.storage.sqlite import SQLiteStore


class SpaceModel(PersonModel):
    def __init__(self):
        self.opportunities = 0

    def react(self, context):
        return self._reaction(context)

    def react_call_for_session(self, context, session_id):
        reaction = self._reaction(context)
        return ModelCallResult(
            value=reaction,
            trace=ModelCallTrace(
                request_messages=[{"role": "user", "content": context}],
                response_text="{}",
                attempt=1,
                model="space-fake",
            ),
        )

    def _reaction(self, context):
        if "SPACE_COMMENT_RECEIVED" in context:
            return PersonReaction(
                actions=[ActionDecision(type=ActionType.SPACE_COMMENT, message="那就一起慢慢来。")]
            )
        if "SPACE_POST_SEEN" in context:
            if "persona c01" in context:
                return PersonReaction(
                    actions=[ActionDecision(type=ActionType.SPACE_COMMENT, message="听起来今天挺需要休息的。")]
                )
            if "persona c02" in context:
                return PersonReaction(actions=[ActionDecision(type=ActionType.SPACE_LIKE)])
        return PersonReaction(actions=[])

    def structured_for_session(self, prompt, schema, session_id):
        assert schema is SpacePostPlan
        self.opportunities += 1
        return SpacePostPlan(
            social_post="今天想安静一点，晚点再做别的。",
            media_intents=[],
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


class FailingMediaModel(SpaceModel):
    def structured_for_session(self, prompt, schema, session_id):
        assert schema is SpacePostPlan
        self.opportunities += 1
        return SpacePostPlan(
            social_post="图如果拿不到也没关系，文字还是想发。",
            media_intents=[
                SpaceMediaIntent(type="SEARCH_IMAGE", query="Tokyo rain night", count=1)
            ],
        )


class WrongChannelModel(SpaceModel):
    def _reaction(self, context):
        return PersonReaction(
            actions=[
                ActionDecision(type=ActionType.MESSAGE, message="这不该进入私聊"),
                ActionDecision(type=ActionType.SPACE_LIKE),
                ActionDecision(type=ActionType.SPACE_COMMENT, message="公开评论"),
            ]
        )


def _access(tmp_path, ids=("c00", "c01", "c02"), model=None):
    store = SQLiteStore(tmp_path / "space-autonomy.db")
    embedding = DeterministicEmbedding()
    model = model or SpaceModel()
    recall = VectorRecall(store, embedding)
    profiles = [
        {"id": character_id, "name": f"角色{character_id[-2:]}", "identity": ""}
        for character_id in ids
    ]
    runtimes = {
        character_id: PersonRuntime(
            store,
            recall,
            embedding,
            model,
            f"persona {character_id}",
        )
        for character_id in ids
    }
    bundle = SimpleNamespace(runtimes=runtimes, model=model)
    access = SimpleNamespace(
        settings=SimpleNamespace(
            api_key="test-key",
            space_autonomy_enabled=True,
            space_opportunity_interval_minutes=1440.0,
            space_audience_size=5,
            space_scheduler_poll_seconds=60.0,
        ),
        read_store=store,
        store=lambda: store,
        character_profiles=lambda: profiles,
        require_bundle=lambda: bundle,
    )
    return access, store, model


def test_space_channel_drops_private_chat_actions(tmp_path):
    access, store, _ = _access(tmp_path, ids=("c01",), model=WrongChannelModel())
    runtime = access.require_bundle().runtimes["c01"]
    now = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)

    result = runtime.handle(
        Event(
            character_id="c01",
            event_type=EventType.SPACE_POST_SEEN,
            event_time=now,
            content="角色00 在空间发布了一条动态：今天想休息。",
            metadata={"conversation_id": "space:test"},
        )
    )

    assert [item.type for item in result.reaction.actions] == [
        ActionType.SPACE_LIKE,
        ActionType.SPACE_COMMENT,
    ]
    assert not any(
        item.event_type == EventType.CHARACTER_MESSAGE
        for item in store.list_events("c01")
    )
    trace = store.get_runtime_trace(result.event.id)
    assert trace["channel_decisions"] == [
        {"type": ActionType.MESSAGE.value, "decision": "DROP_WRONG_CHANNEL"}
    ]
    store.close()


def test_space_autonomy_runs_view_reaction_comment_and_author_reply(tmp_path):
    access, store, _ = _access(tmp_path)
    repository = SpaceRepository(store)
    service = SpaceAutonomyService(access, repository)
    now = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)

    outcome = service.run_opportunity("c00", now=now, cascade=True, source="DEV")

    assert outcome["posted"] is True
    post_id = outcome["post"]["id"]
    assert len(outcome["audience"]) == 2
    assert {item.character_id for item in repository.list_views(post_id)} == {"c01", "c02"}
    assert {item.character_id for item in repository.list_reactions(post_id)} == {"c02"}

    comments = repository.list_comments(post_id)
    assert [(item.character_id, item.content) for item in comments] == [
        ("c01", "听起来今天挺需要休息的。"),
        ("c00", "那就一起慢慢来。"),
    ]
    assert comments[1].reply_to_comment_id == comments[0].id

    # Space reactions went through PersonRuntime but never polluted direct chat.
    assert not any(
        item.event_type == EventType.CHARACTER_MESSAGE
        for character_id in ("c00", "c01", "c02")
        for item in store.list_events(character_id)
    )
    assert any(
        item.event_type == EventType.SPACE_COMMENT_RECEIVED
        for item in store.list_events("c00")
    )
    store.close()


def test_space_media_failure_does_not_block_text_post(tmp_path):
    access, store, _ = _access(tmp_path, ids=("c00",), model=FailingMediaModel())
    repository = SpaceRepository(store)
    service = SpaceAutonomyService(access, repository)
    now = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)

    outcome = service.run_opportunity("c00", now=now, cascade=False, source="DEV")

    assert outcome["posted"] is True
    assert outcome["post"]["content"] == "图如果拿不到也没关系，文字还是想发。"
    assert outcome["post"]["media_count"] == 0
    assert outcome["media_errors"]
    assert outcome["media_errors"][0]["type"] == "SEARCH_IMAGE"
    store.close()


def test_interval_scheduler_runs_again_after_one_hour_and_manual_dev_does_not_consume_it(tmp_path):
    access, store, model = _access(tmp_path, ids=("c00",))
    access.settings.space_opportunity_interval_minutes = 60
    repository = SpaceRepository(store)
    scheduler = SpaceAutonomyScheduler(access, repository, poll_seconds=10)
    start = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)

    # First status arms the new character one interval into the future.
    status = scheduler.status(start)
    assert status["interval_minutes"] == 60
    assert status["characters"][0]["next_opportunity_at"].startswith("2026-09-21T21:00")

    assert scheduler.run_once(start) == []
    first = scheduler.run_once(datetime(2026, 9, 21, 21, 0, tzinfo=timezone.utc))
    assert len(first) == 1
    assert model.opportunities == 1

    # Manual Dev testing is independent from the formal next opportunity.
    before = repository.get_opportunity_state("c00")["next_opportunity_at"]
    manual = scheduler.service.run_opportunity(
        "c00",
        now=datetime(2026, 9, 21, 21, 10, tzinfo=timezone.utc),
        cascade=True,
        source="DEV",
    )
    assert manual["posted"] is True
    assert model.opportunities == 2
    assert repository.get_opportunity_state("c00")["next_opportunity_at"] == before

    assert scheduler.run_once(datetime(2026, 9, 21, 21, 59, tzinfo=timezone.utc)) == []
    second = scheduler.run_once(datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc))
    assert len(second) == 1
    assert model.opportunities == 3
    assert len(repository.list_opportunity_runs(character_id="c00")) == 2
    store.close()

def test_daily_post_ceiling_skips_without_moving_the_next_opportunity(tmp_path):
    """A spent publishing budget pauses scheduling; it never reschedules it.

    Times are mid-day UTC so that the local day the ceiling is measured over is
    the same one every CI timezone lands in.
    """
    access, store, model = _access(tmp_path, ids=("c00",))
    access.settings.space_opportunity_interval_minutes = 30
    access.settings.space_max_posts_per_day = 2
    repository = SpaceRepository(store)
    scheduler = SpaceAutonomyScheduler(access, repository, poll_seconds=10)
    start = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    assert scheduler.status(start)["max_posts_per_day"] == 2
    assert scheduler.run_once(start) == []
    assert len(scheduler.run_once(datetime(2026, 9, 21, 12, 30, tzinfo=timezone.utc))) == 1
    assert len(scheduler.run_once(datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc))) == 1
    assert model.opportunities == 2

    before = repository.get_opportunity_state("c00")["next_opportunity_at"]
    assert scheduler.run_once(datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc)) == []
    assert model.opportunities == 2
    assert repository.get_opportunity_state("c00")["next_opportunity_at"] == before
    store.close()


def test_dev_console_exposes_space_autonomy_controls():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    html = (web / "dev.html").read_text(encoding="utf-8")
    script = (web / "dev.js").read_text(encoding="utf-8")
    server = (root / "src" / "character_memory" / "dev_server.py").read_text(encoding="utf-8")

    for token in [
        'id="spaceCharacter"',
        'id="runSpaceOpportunity"',
        'id="runSpaceAudience"',
        'id="refreshSpaceStatus"',
        'id="spacePostId"',
        'id="spaceIntervalMinutes"',
        'id="spaceMaxPostsPerDay"',
        'id="spaceMediaEnabled"',
        'id="spaceMediaMaxItems"',
        'id="spaceImageSearchEnabled"',
        'id="spaceImageGenerationEnabled"',
        'id="spaceMediaType"',
        'id="runSpaceMedia"',
        'id="spaceAudienceSize"',
        'id="spacePollSeconds"',
        'id="applySpaceConfig"',
        'id="forceSpaceDue"',
        'id="spaceStatusAge"',
        'data-minutes="60"',
    ]:
        assert token in html

    # A console that lost its script renders the markup's placeholder values and
    # looks alive while nothing responds. The page has to say so, and dev.js has
    # to be the thing that clears the warning.
    assert 'id="bootWarning"' in html
    assert 'class="boot-warning"' in html
    assert "dataset.devBooted" in html
    assert 'document.body.dataset.devBooted = "1"' in script
    assert '.boot-warning' in (web / "dev.css").read_text(encoding="utf-8")
    for token in [
        "/v1/dev/space/status",
        "/v1/dev/space/opportunity/",
        "/v1/dev/space/audience/",
        "/v1/dev/space/media/",
        "/v1/dev/space/config",
        "/v1/dev/space/due/",
    ]:
        assert token in script
        assert token in server


def test_space_behavior_uses_configurable_interval_and_audience_size(tmp_path):
    access, store, _ = _access(tmp_path, ids=("c00", "c01", "c02"))
    access.settings.space_opportunity_interval_minutes = 30
    access.settings.space_audience_size = 1

    repository = SpaceRepository(store)
    scheduler = SpaceAutonomyScheduler(access, repository, poll_seconds=20)
    now = datetime(2026, 9, 21, 5, 0, tzinfo=timezone.utc)

    status = scheduler.status(now)
    assert status["interval_minutes"] == 30
    assert status["characters"][0]["next_opportunity_at"].startswith("2026-09-21T05:30")
    assert status["audience_size"] == 1
    assert status["poll_seconds"] == 20

    post = repository.create_post("c00", "测试配置化 Audience", now)
    assert len(scheduler.service.select_audience(post.id, "c00")) == 1

    scheduler.apply_runtime_config(interval_minutes=60, audience_size=2, poll_seconds=10, now=now)
    status = scheduler.status(now)
    assert status["interval_minutes"] == 60
    assert status["audience_size"] == 2
    assert status["characters"][0]["next_opportunity_at"].startswith("2026-09-21T06:00")
    store.close()
