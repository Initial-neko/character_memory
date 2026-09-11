from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.application.async_conversation import (
    ConversationEventHub,
    ReactionScheduler,
    direct_channel,
)
from character_memory.application.chat_service import build_user_event
from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import (
    GroupConversationService,
    SupersededGroupReaction,
)
from character_memory.async_web import attach_async_routes
from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    Event,
    EventType,
    MemoryCandidate,
    PersonReaction,
)
from character_memory.group_web import attach_group_routes
from character_memory.llm.client import ModelCallResult, ModelCallTrace, PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime, SupersededReaction
from character_memory.storage.sqlite import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


class DerivedWriteModel(PersonModel):
    def _reaction(self):
        return PersonReaction(
            perception="看到了新的连续消息",
            reaction="准备回应",
            mental_state_update="准备回应用户",
            actions=[ActionDecision(type=ActionType.MESSAGE, message="我看到了")],
            memory_candidates=[MemoryCandidate(content="用户正在连续说明问题", memory_type="SHARED", importance=0.8)],
        )

    def react(self, context):
        return self._reaction()

    def react_call_for_session(self, context, session_id):
        return ModelCallResult(
            value=self._reaction(),
            trace=ModelCallTrace(
                request_messages=[{"role":"user","content":context}],
                response_text='{"actions":[{"type":"MESSAGE"}]}',
                attempt=1,
                model="fake-async",
            ),
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def test_superseded_direct_reaction_cannot_commit_derived_state(tmp_path):
    store = SQLiteStore(tmp_path / "superseded.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, DerivedWriteModel(), "persona")
    now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
    source = store.append_event(
        Event(
            character_id="rin",
            event_type=EventType.USER_MESSAGE,
            event_time=now,
            content="第一条",
            metadata={"conversation_id":"direct-1"},
        )
    )

    with pytest.raises(SupersededReaction):
        runtime.handle(source, persist_event=False, commit_guard=lambda: False)

    assert [event.event_type for event in store.list_events("rin")] == [EventType.USER_MESSAGE]
    assert store.get_mental_state("rin") == ""
    assert store.list_memories("rin") == []
    assert store.list_runtime_trace_sources("rin") == set()
    store.close()


class _LockService:
    def __init__(self):
        self.lock = threading.RLock()

    def _lock_for(self, character_id):
        return self.lock


class RecordingRuntime:
    def __init__(self):
        self.calls = []
        self.done = threading.Event()

    def handle(self, event, *, image_data_urls=None, persist_event=True, commit_guard=None):
        assert persist_event is False
        assert commit_guard is None or commit_guard()
        self.calls.append((event.id, list(image_data_urls or [])))
        self.done.set()
        return SimpleNamespace(reaction=SimpleNamespace(actions=[], perception="", reaction=""))


def _append_direct(store, content, at, conversation_id="conv"):
    return store.append_event(
        build_user_event(
            content,
            character_id="rin",
            conversation_id=conversation_id,
            at=at,
        )
    )


def test_scheduler_coalesces_quick_user_burst_into_latest_reaction(tmp_path):
    store = SQLiteStore(tmp_path / "burst.db")
    runtime = RecordingRuntime()
    bundle = SimpleNamespace(store=store, runtimes={"rin":runtime}, chat=_LockService())
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(lambda:bundle, lambda:[], hub, quiet_seconds=0.05, max_burst_seconds=0.1)
    now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
    first = _append_direct(store, "我先说第一句", now)
    second = _append_direct(store, "再补充第二句", now + timedelta(milliseconds=10))

    scheduler.enqueue_direct("rin", "conv", first, image_data_url="data:image/png;base64,first")
    scheduler.enqueue_direct("rin", "conv", second, image_data_url="data:image/png;base64,second")

    assert runtime.done.wait(1.5)
    assert runtime.calls == [
        (second.id, ["data:image/png;base64,first", "data:image/png;base64,second"])
    ]
    scheduler.close()
    hub.close()
    store.close()


class BlockingSupersedeRuntime:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        self.committed = []
        self.images_by_event = {}
        self.calls = 0

    def handle(self, event, *, image_data_urls=None, persist_event=True, commit_guard=None):
        self.calls += 1
        self.images_by_event[event.id] = list(image_data_urls or [])
        if self.calls == 1:
            self.started.set()
            assert self.release.wait(1.5)
        if commit_guard is not None and not commit_guard():
            raise SupersededReaction("stale")
        self.committed.append(event.id)
        self.done.set()
        return SimpleNamespace(reaction=SimpleNamespace(actions=[], perception="", reaction=""))


def test_new_message_supersedes_inflight_generation_without_losing_prior_image(tmp_path):
    store = SQLiteStore(tmp_path / "race.db")
    runtime = BlockingSupersedeRuntime()
    bundle = SimpleNamespace(store=store, runtimes={"rin":runtime}, chat=_LockService())
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(lambda:bundle, lambda:[], hub, quiet_seconds=0.01, max_burst_seconds=0.02)
    now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
    first = _append_direct(store, "先看这张图", now)
    scheduler.enqueue_direct("rin", "conv", first, image_data_url="data:image/png;base64,first")
    assert runtime.started.wait(1.0)

    second = _append_direct(store, "补充一句", now + timedelta(seconds=1))
    scheduler.enqueue_direct("rin", "conv", second, image_data_url="data:image/png;base64,second")
    runtime.release.set()

    assert runtime.done.wait(2.0)
    assert runtime.committed == [second.id]
    assert runtime.images_by_event[second.id] == [
        "data:image/png;base64,first",
        "data:image/png;base64,second",
    ]
    channel_events = list(hub._channel(direct_channel("rin", "conv")).events)
    assert any(event_type == "reaction_status" and data.get("state") == "superseded" for _, event_type, data in channel_events)
    scheduler.close()
    hub.close()
    store.close()


class GroupSequenceModel(PersonModel):
    def __init__(self, reactions):
        self.reactions = list(reactions)
        self.contexts = []

    def react(self, context):
        return self.react_for_session(context, "default")

    def react_for_session(self, context, session_id):
        self.contexts.append(context)
        return self.reactions.pop(0)

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def _group_runtime(store, emb, model, persona):
    return PersonRuntime(store, VectorRecall(store, emb), emb, model, persona)


def test_group_reaction_can_be_superseded_after_model_before_any_commit(tmp_path):
    store = SQLiteStore(tmp_path / "group-stale.db")
    emb = DeterministicEmbedding()
    model = GroupSequenceModel([
        PersonReaction(
            mental_state_update="会被丢弃",
            actions=[ActionDecision(type=ActionType.MESSAGE, message="旧回复")],
            memory_candidates=[MemoryCandidate(content="不应该写入", importance=0.9)],
        )
    ])
    service = GroupConversationService(
        store,
        {"rin":_group_runtime(store, emb, model, "Rin"), "momo":_group_runtime(store, emb, model, "Momo")},
        FixedClock(datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)),
        profiles=[{"id":"rin","name":"Rin"},{"id":"momo","name":"Momo"}],
    )
    group = service.create_group("异步群", ["rin","momo"])
    source = service.persist_user_event(group.id, "第一句话")
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        return checks == 1

    with pytest.raises(SupersededGroupReaction):
        service.react_from_event(source, commit_guard=guard)

    events = service.repo.list_events(group.id)
    assert [(event.actor_type, event.actor_id) for event in events] == [("USER","user")]
    assert store.get_mental_state("rin") == ""
    assert store.list_memories("rin") == []
    assert service.repo.list_turn_traces(group.id, source.turn_id) == []
    store.close()


def test_group_progressive_callback_runs_after_each_member_commit(tmp_path):
    store = SQLiteStore(tmp_path / "group-progressive.db")
    emb = DeterministicEmbedding()
    model = GroupSequenceModel([
        PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="Rin先说")]),
        PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="Momo接话")]),
    ])
    service = GroupConversationService(
        store,
        {"rin":_group_runtime(store, emb, model, "Rin"), "momo":_group_runtime(store, emb, model, "Momo")},
        FixedClock(datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)),
        profiles=[{"id":"rin","name":"Rin"},{"id":"momo","name":"Momo"}],
    )
    group = service.create_group("渐进群", ["rin","momo"])
    source = service.persist_user_event(group.id, "你们怎么看")
    callbacks = []

    def on_member(decision):
        callbacks.append((decision["character_id"], [event.actor_id for event in service.repo.list_events(group.id)]))

    service.react_from_event(source, on_member=on_member)

    assert [item[0] for item in callbacks] == ["rin", "momo"]
    assert callbacks[0][1] == ["user", "rin"]
    assert callbacks[1][1] == ["user", "rin", "momo"]
    assert "Rin: Rin先说" in model.contexts[1]
    store.close()


def test_async_message_routes_persist_without_initializing_llm(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            'api_key: ""',
            'embedding_provider: "deterministic"',
            f'db_path: "{(tmp_path / "async-api.db").as_posix()}"',
            f'persona_path: "{(ROOT / "personas" / "rin" / "persona.yaml").as_posix()}"',
        ]),
        encoding="utf-8",
    )
    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_async_routes(app)
    # Keep the worker in its burst window for this API acceptance test. The
    # endpoint itself must not initialize the missing-key runtime.
    app.state.character_memory.reaction_scheduler.quiet_seconds = 60
    app.state.character_memory.reaction_scheduler.max_burst_seconds = 60

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        character_id = profiles[0]["id"]
        direct = client.post(
            "/v1/chat/messages",
            json={"message":"连续聊天第一句", "character_id":character_id, "conversation_id":"async-test"},
        )
        assert direct.status_code == 202, direct.text
        assert direct.json()["accepted"] is True
        assert client.get("/health").json()["runtime_loaded"] is False
        history = client.get(f"/v1/chat/history?character_id={character_id}&limit=10").json()["messages"]
        assert history[-1]["content"] == "连续聊天第一句"

        group = client.post(
            "/v1/groups",
            json={"name":"异步测试群", "member_ids":[profiles[0]["id"], profiles[1]["id"]]},
        ).json()["group"]
        group_send = client.post(f"/v1/groups/{group['id']}/messages", json={"message":"群里先落库"})
        assert group_send.status_code == 202, group_send.text
        assert client.get("/health").json()["runtime_loaded"] is False
        group_history = client.get(f"/v1/groups/{group['id']}/history?limit=10").json()["messages"]
        assert group_history[-1]["content"] == "群里先落库"


def test_web_composer_stays_enabled_and_direct_group_use_async_message_routes():
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    groups_js = (WEB / "groups.js").read_text(encoding="utf-8")
    stickers_js = (WEB / "stickers.js").read_text(encoding="utf-8")
    images_js = (WEB / "images.js").read_text(encoding="utf-8")

    assert 'CM.dom.input.disabled = false' in app_js
    assert 'CM.dom.sendButton.disabled = false' in app_js
    assert '/v1/chat/messages' in app_js
    assert 'new EventSource(' in app_js
    assert 'CM.state.pendingCharacters.has(sentCharacter)) return' not in app_js

    assert '/v1/groups/${encodeURIComponent(groupId)}/messages' in groups_js
    assert 'new EventSource(' in groups_js
    assert 'CM.dom.input.disabled = false' in groups_js
    assert 'pending.has(groupId)) return' not in groups_js
    assert 'group_character_event' in groups_js

    assert 'CM.sendDirectPayload({message:"", sticker_id:sticker.id})' in stickers_js
    assert 'CM.sendDirectPayload({' in images_js

    node = shutil.which("node")
    if node:
        for path in [WEB / "app.js", WEB / "groups.js", WEB / "stickers.js", WEB / "images.js"]:
            checked = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            assert checked.returncode == 0, f"{path.name}: {checked.stderr}"
