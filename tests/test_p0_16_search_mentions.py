from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.application.async_conversation import ConversationEventHub, ReactionScheduler, _PendingState
from character_memory.application.chat_service import build_user_event
from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import (
    GroupConversationService,
    build_group_user_event,
    resolve_group_mentions,
)
from character_memory.domain.models import DailyLifePlan, DiaryResult, Event, EventType, PersonReaction
from character_memory.group_store import GroupRepository
from character_memory.group_web import attach_group_routes
from character_memory.llm.client import ModelCallResult, ModelCallTrace, PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.search_web import attach_search_routes
from character_memory.storage.chat_history import ChatHistoryRepository
from character_memory.storage.message_search import MessageSearchRepository
from character_memory.storage.sqlite import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


class MentionModel(PersonModel):
    def __init__(self):
        self.contexts = []

    def react(self, context):
        return PersonReaction()

    def react_call_for_session(self, context, session_id):
        self.contexts.append((session_id, context))
        return ModelCallResult(
            value=PersonReaction(perception="看到群聊", reaction="判断是否接话", actions=[]),
            trace=ModelCallTrace(
                request_messages=[{"role":"user", "content":context}],
                response_text='{"actions":[]}',
                attempt=1,
                model="mention-fake",
            ),
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def _runtime(store, emb, model, persona):
    return PersonRuntime(store, VectorRecall(store, emb), emb, model, persona)


def test_resolve_group_mentions_uses_stable_ids_and_supports_everyone():
    profiles = {
        "rei":{"id":"rei", "name":"Rei"},
        "momo":{"id":"momo", "name":"桃桃"},
        "rin":{"id":"rin", "name":"Rin"},
    }
    members = ["rei", "momo", "rin"]

    assert resolve_group_mentions(members, "@桃桃 你怎么看？ @Rei 也说说", profiles) == ["momo", "rei"]
    assert resolve_group_mentions(members, "@所有人 一起看看", profiles) == ["*"]
    assert resolve_group_mentions(members, "普通消息", profiles, ["rin"]) == ["rin"]
    with pytest.raises(ValueError, match="Unknown mentioned character"):
        resolve_group_mentions(members, "hello", profiles, ["ghost"])


def test_group_mentions_prioritize_named_member_but_everyone_still_judges(tmp_path):
    store = SQLiteStore(tmp_path / "mentions.db")
    emb = DeterministicEmbedding()
    model = MentionModel()
    profiles = [
        {"id":"rei", "name":"Rei"},
        {"id":"momo", "name":"桃桃"},
        {"id":"rin", "name":"Rin"},
    ]
    runtimes = {item["id"]:_runtime(store, emb, model, item["name"]) for item in profiles}
    service = GroupConversationService(
        store,
        runtimes,
        FixedClock(datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc)),
        profiles=profiles,
    )
    group = service.create_group("点名测试", ["rei", "momo", "rin"])
    source = service.persist_user_event(group.id, "@桃桃 你怎么看？")

    assert source.metadata["mentions"] == ["momo"]
    result = service.react_from_event(source)
    assert result["speaker_order"][0] == "momo"
    assert set(result["speaker_order"]) == {"rei", "momo", "rin"}
    assert len(result["decisions"]) == 3

    traces = {item["character_id"]:item for item in service.repo.list_turn_traces(group.id, source.turn_id)}
    assert traces["momo"]["explicitly_mentioned"] is True
    assert traces["rei"]["explicitly_mentioned"] is False
    assert traces["rin"]["explicitly_mentioned"] is False
    assert "明确 @ 了你" in model.contexts[0][1]
    assert any("主要 @ 了 桃桃" in context for _, context in model.contexts[1:])
    store.close()


def test_burst_mentions_merge_in_first_seen_order_and_everyone_wins(tmp_path):
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(lambda:SimpleNamespace(), lambda:[], hub)
    state = _PendingState()
    state.mention_by_event = {11:["rei"], 12:["momo"], 13:["rei", "rin"]}
    assert scheduler._ordered_mentions(state, 13) == ["rei", "momo", "rin"]
    state.mention_by_event[14] = ["*"]
    assert scheduler._ordered_mentions(state, 14) == ["*"]
    scheduler.close()
    hub.close()


def test_message_search_is_literal_and_only_reads_chat_events(tmp_path):
    store = SQLiteStore(tmp_path / "search.db")
    now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
    store.append_event(build_user_event("SQLite 100% 完成", character_id="rin", conversation_id="c1", at=now))
    store.append_event(build_user_event("SQLite 100x 完成", character_id="momo", conversation_id="c2", at=now + timedelta(seconds=1)))
    repository = MessageSearchRepository(store)

    assert [item["character_id"] for item in repository.search_direct("100%", limit=10)] == ["rin"]
    assert {item["character_id"] for item in repository.search_direct("SQLite", limit=10)} == {"rin", "momo"}
    store.close()


def test_search_jump_cursor_reuses_history_page_and_keeps_hit_in_window(tmp_path):
    store = SQLiteStore(tmp_path / "jump.db")
    start = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    target = None
    for index in range(80):
        event = store.append_event(
            build_user_event(
                f"message-{index}{' UNIQUE-NEEDLE' if index == 15 else ''}",
                character_id="rin",
                conversation_id="jump",
                at=start + timedelta(minutes=index),
            )
        )
        if index == 15:
            target = event

    search = MessageSearchRepository(store)
    hit = search.search_direct("UNIQUE-NEEDLE", character_id="rin", limit=10)[0]
    before_id = search.jump_before_direct("rin", hit["event_id"])
    assert before_id is not None
    page = ChatHistoryRepository(store).list_page("rin", limit=50, before_id=before_id)
    assert target.id in [event.id for event in page.events]
    store.close()


def test_global_search_http_merges_direct_and_group_results(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            'api_key: ""',
            'embedding_provider: "deterministic"',
            f'db_path: "{(tmp_path / "search-api.db").as_posix()}"',
            f'persona_path: "{(ROOT / "personas" / "rin" / "persona.yaml").as_posix()}"',
        ]),
        encoding="utf-8",
    )
    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_search_routes(app)

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        first, second = profiles[0]["id"], profiles[1]["id"]
        store = app.state.character_memory.store()
        now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
        store.append_event(build_user_event("needle direct", character_id=first, conversation_id="direct-search", at=now))
        group = client.post("/v1/groups", json={"name":"Search Group", "member_ids":[first, second]}).json()["group"]
        repository = GroupRepository(store)
        repository.append_event(build_group_user_event(group["id"], "needle group", at=now + timedelta(seconds=1)))

        response = client.get("/v1/search/messages", params={"q":"needle", "scope":"global"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert {item["scope"] for item in data["results"]} == {"DIRECT", "GROUP"}
        group_hit = next(item for item in data["results"] if item["scope"] == "GROUP")
        assert group_hit["conversation_name"] == "Search Group"
        assert group_hit["actor_name"] == "我"

        current = client.get("/v1/search/messages", params={"q":"needle", "scope":"direct", "character_id":first})
        assert current.status_code == 200
        assert all(item["scope"] == "DIRECT" for item in current.json()["results"])


def test_group_async_contract_accepts_structured_mentions_without_runtime_init(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            'api_key: ""',
            'embedding_provider: "deterministic"',
            f'db_path: "{(tmp_path / "mention-api.db").as_posix()}"',
            f'persona_path: "{(ROOT / "personas" / "rin" / "persona.yaml").as_posix()}"',
        ]),
        encoding="utf-8",
    )
    from character_memory.async_web import attach_async_routes

    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_async_routes(app)
    app.state.character_memory.reaction_scheduler.quiet_seconds = 60
    app.state.character_memory.reaction_scheduler.max_burst_seconds = 60

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        first, second = profiles[0], profiles[1]
        group = client.post("/v1/groups", json={"name":"Mention API", "member_ids":[first["id"], second["id"]]}).json()["group"]
        response = client.post(
            f"/v1/groups/{group['id']}/messages",
            json={"message":f"@{second['name']} 看看这个", "mentions":[second["id"]]},
        )
        assert response.status_code == 202, response.text
        assert response.json()["message"]["mentions"] == [second["id"]]
        assert client.get("/health").json()["runtime_loaded"] is False


def test_p0_16_frontend_modules_have_search_jump_and_mention_autocomplete_contracts():
    search_js = (WEB / "search.js").read_text(encoding="utf-8")
    mentions_js = (WEB / "mentions.js").read_text(encoding="utf-8")
    index_html = (WEB / "index.html").read_text(encoding="utf-8")

    assert "/v1/search/messages" in search_js
    assert "jump_before_id" in search_js
    assert "loadDirectHistory({beforeId:item.jump_before_id})" in search_js
    assert "groups?.loadHistory?.({beforeId:item.jump_before_id})" in search_js
    assert "@所有人" in mentions_js
    assert "mentionsForText" in mentions_js
    assert "/messages" in mentions_js
    assert 'id="conversationSearchButton"' in index_html
    # Current UI intentionally has one Search entry; the drawer switches between
    # current-conversation and global scope instead of exposing a redundant button.
    assert 'id="globalSearchButton"' not in index_html
    assert '/static/mentions.js' in index_html
    assert '/static/search.js' in index_html

    node = shutil.which("node")
    if node:
        subprocess.run([node, "--check", str(WEB / "search.js")], check=True, capture_output=True, text=True)
        subprocess.run([node, "--check", str(WEB / "mentions.js")], check=True, capture_output=True, text=True)
