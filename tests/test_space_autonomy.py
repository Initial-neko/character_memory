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
        assert schema is DailyLifePlan
        self.opportunities += 1
        return DailyLifePlan(
            events=[],
            social_post="今天想安静一点，晚点再做别的。",
            image_prompt=None,
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


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
            space_daily_window_start_hour=18,
            space_daily_window_end_hour=22,
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


def test_daily_scheduler_runs_once_but_dev_opportunity_does_not_consume_it(tmp_path):
    access, store, model = _access(tmp_path, ids=("c00",))
    repository = SpaceRepository(store)
    scheduler = SpaceAutonomyScheduler(access, repository, poll_seconds=60)
    now = datetime(2026, 9, 21, 23, 30, tzinfo=timezone.utc)

    first = scheduler.run_once(now)
    second = scheduler.run_once(now)

    assert len(first) == 1
    assert second == []
    assert model.opportunities == 1
    run = repository.get_daily_run("c00", "2026-09-21")
    assert run["status"] == "POSTED"
    daily_post_id = run["post_id"]

    manual = scheduler.service.run_opportunity(
        "c00",
        now=now,
        cascade=True,
        source="DEV",
    )
    assert manual["posted"] is True
    assert model.opportunities == 2
    assert repository.get_daily_run("c00", "2026-09-21")["post_id"] == daily_post_id
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
    ]:
        assert token in html
    for token in [
        "/v1/dev/space/status",
        "/v1/dev/space/opportunity/",
        "/v1/dev/space/audience/",
    ]:
        assert token in script
        assert token in server


def test_space_behavior_uses_webui_configurable_window_and_audience_size(tmp_path):
    access, store, _ = _access(tmp_path, ids=("c00", "c01", "c02"))
    access.settings.space_daily_window_start_hour = 6
    access.settings.space_daily_window_end_hour = 7
    access.settings.space_audience_size = 1

    repository = SpaceRepository(store)
    scheduler = SpaceAutonomyScheduler(access, repository, poll_seconds=30)
    now = datetime(2026, 9, 21, 5, 0, tzinfo=timezone.utc)

    scheduled = scheduler.scheduled_for("c00", now)
    assert scheduled.hour == 6
    assert 0 <= scheduled.minute <= 59

    post = repository.create_post("c00", "测试配置化 Audience", now)
    audience = scheduler.service.select_audience(post.id, "c00")
    assert len(audience) == 1

    status = scheduler.status(now)
    assert status["window"] == "06:00-07:00 local time"
    assert status["audience_size"] == 1
    assert status["poll_seconds"] == 30
    store.close()
