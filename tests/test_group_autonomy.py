from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from character_memory.application.clock import FixedClock
from character_memory.application.group_autonomy import (
    GroupAutonomyScheduler,
    GroupAutonomyService,
)
from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    PersonReaction,
)
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.llm.client import ModelCallResult, ModelCallTrace, PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class AutonomousGroupModel(PersonModel):
    def __init__(self, actions_by_character=None, on_call=None):
        self.actions_by_character = actions_by_character or {}
        self.on_call = on_call
        self.contexts = []
        self.calls = []

    def _reaction(self, context, session_id):
        character_id = session_id.rsplit(":", 1)[-1]
        self.contexts.append(context)
        self.calls.append(character_id)
        if self.on_call is not None:
            callback = self.on_call
            self.on_call = None
            callback()
        actions = self.actions_by_character.get(character_id, [])
        return PersonReaction(actions=actions)

    def react(self, context):
        return PersonReaction(actions=[])

    def react_call_for_session(self, context, session_id):
        value = self._reaction(context, session_id)
        return ModelCallResult(
            value=value,
            trace=ModelCallTrace(
                request_messages=[{"role":"user","content":context}],
                response_text="{}",
                attempt=1,
                model="group-autonomy-fake",
            ),
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def _access(tmp_path, *, ids=("c00", "c01"), model=None, now=None):
    now = now or datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "group-autonomy.db")
    embeddings = DeterministicEmbedding()
    model = model or AutonomousGroupModel()
    recall = VectorRecall(store, embeddings)
    profiles = [
        {"id": character_id, "name": f"角色{character_id[-2:]}", "identity": ""}
        for character_id in ids
    ]
    runtimes = {
        character_id: PersonRuntime(
            store,
            recall,
            embeddings,
            model,
            f"persona {character_id}",
        )
        for character_id in ids
    }
    clock = FixedClock(now)
    bundle = SimpleNamespace(
        store=store,
        runtimes=runtimes,
        clock=clock,
        chat=None,
        model=model,
        embeddings=embeddings,
    )
    settings = SimpleNamespace(
        api_key="test-key",
        group_autonomy_enabled=True,
        group_autonomy_interval_minutes=60.0,
        group_autonomy_max_messages=3,
        group_autonomy_poll_seconds=60.0,
        group_autonomy_user_quiet_minutes=30.0,
    )
    access = SimpleNamespace(
        settings=settings,
        read_store=store,
        store=lambda: store,
        require_bundle=lambda: bundle,
        character_profiles=lambda: profiles,
    )
    return access, store, model, profiles


def _group(store, ids, now):
    return GroupRepository(store).create_group("自主测试群", list(ids), now)


def test_autonomous_group_seed_starts_one_bounded_shared_turn(tmp_path):
    model = AutonomousGroupModel(
        {
            "c00": [ActionDecision(type=ActionType.MESSAGE, message="突然想起来，今天风还挺舒服。")],
            "c01": [ActionDecision(type=ActionType.MESSAGE, message="嗯，适合出去走一圈。")],
            "c02": [ActionDecision(type=ActionType.MESSAGE, message="我也想下楼买点东西。")],
        }
    )
    access, store, _, _ = _access(tmp_path, ids=("c00", "c01", "c02"), model=model)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    group = _group(store, ("c00", "c01", "c02"), now)
    service = GroupAutonomyService(access, GroupRepository(store))

    outcome = service.run_opportunity(group.id, now=now, source="DEV")

    assert outcome["status"] == "CHATTED"
    assert outcome["message_count"] == 3
    assert len(outcome["events"]) == 3
    assert all(item.metadata["autonomous"] is True for item in outcome["events"])
    assert all(item.metadata["source"] == "GROUP_AUTONOMY" for item in outcome["events"])

    all_events = GroupRepository(store).list_events(group.id)
    assert all_events[0].actor_type == "SYSTEM"
    assert all_events[0].metadata["hidden"] is True
    visible = GroupRepository(store).list_event_page(group.id, limit=20).events
    assert [item.actor_type for item in visible] == ["CHARACTER", "CHARACTER", "CHARACTER"]
    assert "Autonomous Group Conversation Contract" in model.contexts[0]
    assert "不是 User 刚发来消息" in model.contexts[0]
    store.close()


def test_seed_silence_ends_opportunity_without_waking_every_member(tmp_path):
    model = AutonomousGroupModel(
        {
            "c00": [],
            "c01": [ActionDecision(type=ActionType.MESSAGE, message="不应该轮到我被强制激活")],
        }
    )
    access, store, _, _ = _access(tmp_path, model=model)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    group = _group(store, ("c00", "c01"), now)

    outcome = GroupAutonomyService(access, GroupRepository(store)).run_opportunity(
        group.id, now=now
    )

    assert outcome["status"] == "NO_CHAT"
    assert outcome["message_count"] == 0
    assert model.calls == ["c00"]
    assert GroupRepository(store).list_event_page(group.id, limit=20).events == []
    store.close()


def test_autonomous_group_hard_caps_visible_messages_and_drops_generate_image(tmp_path):
    model = AutonomousGroupModel(
        {
            "c00": [
                ActionDecision(
                    type=ActionType.GENERATE_IMAGE,
                    image_purpose="SCENE",
                    visual_intent="不应该在后台自主群聊触发生图",
                ),
                ActionDecision(type=ActionType.MESSAGE, message="第一句"),
            ],
            "c01": [ActionDecision(type=ActionType.MESSAGE, message="接一句")],
            "c02": [ActionDecision(type=ActionType.MESSAGE, message="第三个人不该提交")],
        }
    )
    access, store, _, _ = _access(tmp_path, ids=("c00", "c01", "c02"), model=model)
    access.settings.group_autonomy_max_messages = 2
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    group = _group(store, ("c00", "c01", "c02"), now)

    outcome = GroupAutonomyService(access, GroupRepository(store)).run_opportunity(
        group.id, now=now
    )

    # Seed's GENERATE_IMAGE is filtered before persistence. Because autonomous
    # mode keeps at most the first *allowed* action, its following MESSAGE stays.
    assert outcome["message_count"] == 2
    assert [item.content for item in outcome["events"]] == ["第一句", "接一句"]
    assert all(item.metadata["action"] != "GENERATE_IMAGE" for item in outcome["events"])
    assert len(model.calls) == 2
    store.close()


def test_autonomous_group_voice_message_reuses_existing_pending_voice_contract(tmp_path):
    model = AutonomousGroupModel(
        {
            "c00": [
                ActionDecision(type=ActionType.VOICE_MESSAGE, message="懒得打字，我直接说啦。")
            ],
            "c01": [],
        }
    )
    access, store, _, _ = _access(tmp_path, model=model)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    group = _group(store, ("c00", "c01"), now)

    outcome = GroupAutonomyService(access, GroupRepository(store)).run_opportunity(
        group.id, now=now
    )

    event = outcome["events"][0]
    assert event.metadata["action"] == "VOICE_MESSAGE"
    assert event.metadata["voice_status"] == "pending"
    assert event.metadata["autonomous"] is True
    store.close()


def test_new_user_fact_supersedes_inflight_autonomous_group_commit(tmp_path):
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    holder = {}

    def inject_user_fact():
        repo = holder["repo"]
        group_id = holder["group_id"]
        repo.append_event(
            GroupEvent(
                conversation_id=group_id,
                turn_id="turn-user-interrupt",
                actor_type="USER",
                actor_id="user",
                event_type="USER_MESSAGE",
                event_time=now + timedelta(seconds=1),
                content="我回来了",
                metadata={"display_text":"我回来了","mentions":[]},
            )
        )

    model = AutonomousGroupModel(
        {"c00":[ActionDecision(type=ActionType.MESSAGE, message="这条会过期")]},
        on_call=inject_user_fact,
    )
    access, store, _, _ = _access(tmp_path, model=model, now=now)
    repo = GroupRepository(store)
    group = repo.create_group("并发群", ["c00","c01"], now)
    holder.update(repo=repo, group_id=group.id)

    outcome = GroupAutonomyService(access, repo).run_opportunity(group.id, now=now)

    assert outcome["status"] == "SUPERSEDED"
    visible = repo.list_event_page(group.id, limit=20).events
    assert [(item.actor_type, item.content) for item in visible] == [("USER", "我回来了")]
    assert store.get_mental_state("c00") == ""
    assert store.list_memories("c00") == []
    store.close()


def test_scheduler_is_restart_safe_respects_user_quiet_and_rearms(tmp_path):
    model = AutonomousGroupModel(
        {
            "c00":[ActionDecision(type=ActionType.MESSAGE, message="到点说一句")],
            "c01":[],
        }
    )
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    access, store, _, _ = _access(tmp_path, model=model, now=now)
    repo = GroupRepository(store)
    group = repo.create_group("调度群", ["c00","c01"], now)
    scheduler = GroupAutonomyScheduler(access, repo, poll_seconds=60)

    status = scheduler.status(now)
    assert status["groups"][0]["next_opportunity_at"].startswith("2026-09-22T13:00")
    assert scheduler.run_once(now) == []

    scheduler.force_due(group.id, now=now)
    first = scheduler.run_once(now)
    assert first[0]["status"] == "CHATTED"
    state = repo.get_autonomy_state(group.id)
    assert state["last_status"] == "CHATTED"
    assert repo.list_autonomy_runs(conversation_id=group.id)[0]["status"] == "CHATTED"

    user_time = now + timedelta(hours=1)
    repo.append_event(
        GroupEvent(
            conversation_id=group.id,
            turn_id="turn-user",
            actor_type="USER",
            actor_id="user",
            event_type="USER_MESSAGE",
            event_time=user_time,
            content="刚说完一句",
            metadata={"display_text":"刚说完一句","mentions":[]},
        )
    )
    scheduler.force_due(group.id, now=user_time)
    skipped = scheduler.run_once(user_time)
    assert skipped[0]["status"] == "SKIPPED"
    assert skipped[0]["reason"] == "recent user activity"
    assert repo.get_autonomy_state(group.id)["last_status"] == "SKIPPED"

    # New repository/scheduler objects see the same persisted next-opportunity state.
    scheduler2 = GroupAutonomyScheduler(access, GroupRepository(store), poll_seconds=60)
    persisted = scheduler2.status(user_time)["groups"][0]
    assert persisted["next_opportunity_at"] == repo.get_autonomy_state(group.id)["next_opportunity_at"]
    store.close()


def test_archived_character_is_not_autonomous_group_participant(tmp_path):
    model = AutonomousGroupModel(
        {
            "c00":[ActionDecision(type=ActionType.MESSAGE, message="在")],
            "c01":[ActionDecision(type=ActionType.MESSAGE, message="归档角色不该说话")],
            "c02":[ActionDecision(type=ActionType.MESSAGE, message="我接")],
        }
    )
    access, store, _, profiles = _access(tmp_path, ids=("c00","c01","c02"), model=model)
    profiles[1]["archived_at"] = "2026-09-22T11:00:00+00:00"
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    group = _group(store, ("c00","c01","c02"), now)

    outcome = GroupAutonomyService(access, GroupRepository(store)).run_opportunity(
        group.id, now=now
    )

    assert "c01" not in outcome["active_member_ids"]
    assert "c01" not in model.calls
    store.close()


def test_group_autonomy_schema_migration_and_dev_console_contract(tmp_path):
    from pathlib import Path

    store = SQLiteStore(tmp_path / "group-autonomy-migration.db")
    try:
        assert "group/004-autonomy-scheduler" in store.list_schema_migrations()
        repo = GroupRepository(store)
        now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        group = repo.create_group("迁移验证", ["c00", "c01"], now)
        state = repo.ensure_autonomy_state(group.id, now, 60)
        assert state["conversation_id"] == group.id
    finally:
        store.close()

    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    html = (web / "dev.html").read_text(encoding="utf-8")
    script = (web / "dev.js").read_text(encoding="utf-8")
    server = (root / "src" / "character_memory" / "dev_server.py").read_text(encoding="utf-8")
    for token in [
        'id="groupAutonomyEnabled"',
        'id="groupAutonomyInterval"',
        'id="groupAutonomyMaxMessages"',
        'id="groupAutonomyQuietMinutes"',
        'id="groupAutonomyPollSeconds"',
        'id="groupAutonomyGroup"',
        'id="runGroupAutonomyOpportunity"',
        'id="forceGroupAutonomyDue"',
        'id="refreshGroupAutonomyStatus"',
    ]:
        assert token in html
    for token in [
        "/v1/dev/group-autonomy/status",
        "/v1/dev/group-autonomy/config",
        "/v1/dev/group-autonomy/opportunity/",
        "/v1/dev/group-autonomy/due/",
    ]:
        assert token in script
        assert token in server
