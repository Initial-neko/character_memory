from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, PersonReaction
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.runtime.sticker_retrieval import StickerRetriever
from character_memory.stickers import Sticker, StickerCatalog
from character_memory.storage.chat_history import ChatHistoryRepository
from character_memory.storage.sqlite import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


class CountingEmbedding:
    def __init__(self):
        self.batch_calls = 0
        self.embed_calls = 0

    @staticmethod
    def _vector(text: str):
        value = str(text)
        if any(token in value for token in ("开心", "庆祝", "好耶", "鼓掌")):
            return [1.0, 0.0, 0.0]
        if any(token in value for token in ("难过", "悲伤", "哭")):
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    def embed(self, text: str):
        self.embed_calls += 1
        return self._vector(text)

    def embed_many(self, texts: list[str]):
        self.batch_calls += 1
        return [self._vector(text) for text in texts]


def sticker_catalog() -> StickerCatalog:
    return StickerCatalog(
        Path("."),
        [
            Sticker(id="happy", file="happy.png", label="开心庆祝", tags=["开心", "庆祝", "好耶"], description="事情成功以后庆祝", pack_id="x", pack_name="测试"),
            Sticker(id="sad", file="sad.png", label="难过", tags=["悲伤", "哭哭"], description="表达失落和难过", pack_id="x", pack_name="测试"),
        ],
        source="test",
    )


def test_sticker_retriever_limits_model_working_set_and_allows_zero_candidates():
    embeddings = CountingEmbedding()
    catalog = sticker_catalog()
    retriever = StickerRetriever(embeddings, limit=1, semantic_threshold=0.8)

    happy = retriever.retrieve(catalog, "终于修好了，好开心，庆祝一下")
    assert happy is not None
    assert [item.id for item in happy.catalog.stickers] == ["happy"]
    assert happy.matches[0].lexical_score > 0

    unrelated = retriever.retrieve(catalog, "数据库事务隔离级别怎么设计")
    assert unrelated is not None
    assert unrelated.catalog.stickers == []
    assert unrelated.matches == []


def test_sticker_catalog_embeddings_are_shared_between_character_runtimes():
    embeddings = CountingEmbedding()
    catalog = sticker_catalog()
    first = StickerRetriever(embeddings, semantic_threshold=0.8)
    second = StickerRetriever(embeddings, semantic_threshold=0.8)

    first.retrieve(catalog, "开心庆祝")
    second.retrieve(catalog, "难过")

    assert embeddings.batch_calls == 1
    assert embeddings.embed_calls == 2


def test_runtime_drops_sticker_that_was_not_in_retrieved_candidates(tmp_path):
    embeddings = CountingEmbedding()
    store = SQLiteStore(tmp_path / "runtime.db")
    runtime = PersonRuntime(store, None, embeddings, None, "persona", sticker_catalog())
    reaction = PersonReaction(actions=[ActionDecision(type=ActionType.STICKER, sticker_id="sad")])

    sanitized, decisions, _ = runtime._sanitize_resource_actions(reaction, allowed_sticker_ids={"happy"})

    assert sanitized.actions == []
    assert decisions == [{"sticker_id": "sad", "decision": "DROP_NOT_RETRIEVED_STICKER"}]
    store.close()


def test_direct_history_uses_cursor_pages_and_trace_lookup_only_for_requested_ids(tmp_path):
    store = SQLiteStore(tmp_path / "history.db")
    repo = ChatHistoryRepository(store)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    saved_ids = []
    for index in range(120):
        event = store.append_event(
            Event(
                character_id="rin",
                event_type=EventType.USER_MESSAGE,
                event_time=start + timedelta(minutes=index),
                content=f"m-{index}",
            )
        )
        saved_ids.append(event.id)

    first = repo.list_page("rin", limit=50)
    assert len(first.events) == 50
    assert first.has_more is True
    assert [event.content for event in first.events[:2]] == ["m-70", "m-71"]
    assert first.events[-1].content == "m-119"

    second = repo.list_page("rin", limit=50, before_id=first.next_before_id)
    assert len(second.events) == 50
    assert second.events[0].content == "m-20"
    assert second.events[-1].content == "m-69"
    assert {event.id for event in first.events}.isdisjoint({event.id for event in second.events})

    store.add_runtime_trace("rin", int(saved_ids[110]), start, {"x": 1})
    store.add_runtime_trace("rin", int(saved_ids[10]), start, {"x": 2})
    assert repo.trace_sources("rin", [saved_ids[110]]) == {saved_ids[110]}
    store.close()


def test_group_history_pages_and_reaction_summary(tmp_path):
    store = SQLiteStore(tmp_path / "group.db")
    repo = GroupRepository(store)
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    group = repo.create_group("测试", ["rin", "momo"], now)

    first_source_id = None
    first_turn = "turn-0"
    for index in range(70):
        event = repo.append_event(
            GroupEvent(
                conversation_id=group.id,
                turn_id=f"turn-{index}",
                actor_type="USER",
                actor_id="user",
                event_type="USER_MESSAGE",
                event_time=now + timedelta(minutes=index),
                content=f"g-{index}",
            )
        )
        if index == 0:
            first_source_id = event.id

    page = repo.list_event_page(group.id, limit=50)
    assert len(page.events) == 50
    assert page.events[0].content == "g-20"
    assert page.events[-1].content == "g-69"
    assert page.has_more is True

    repo.add_trace(group.id, first_turn, "rin", int(first_source_id), now, {"actions": [{"type": "MESSAGE"}]})
    repo.add_trace(group.id, first_turn, "momo", int(first_source_id), now, {"actions": []})
    assert repo.turn_summaries(group.id, [first_turn])[first_turn] == {"total": 2, "replied": 1, "silent": 1}
    store.close()


def test_web_uses_50_message_working_set_and_async_group_incremental_delivery():
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    groups_js = (WEB / "groups.js").read_text(encoding="utf-8")
    group_web = (ROOT / "src" / "character_memory" / "group_web.py").read_text(encoding="utf-8")
    async_web = (ROOT / "src" / "character_memory" / "async_web.py").read_text(encoding="utf-8")
    group_service = (ROOT / "src" / "character_memory" / "application" / "group_conversation_service.py").read_text(encoding="utf-8")

    assert "/v1/chat/history-page" in app_js
    assert 'limit:"50"' in app_js
    assert "limit=180" not in app_js
    assert 'data-load-older-direct' in app_js

    assert 'limit:"50"' in groups_js
    assert "limit=180" not in groups_js
    assert '/v1/groups/${encodeURIComponent(groupId)}/messages' in groups_js
    assert "mergeMessage(result.message)" in groups_js
    assert 'source.addEventListener("group_character_event"' in groups_js
    assert "本轮反应 · ${summary.replied || 0} 回复 / ${summary.silent || 0} 沉默" in groups_js
    assert "result.new_messages" not in groups_js
    assert "new_messages" in group_web
    assert 'list_event_page(conversation_id, limit=limit, before_id=before_id)' in group_web
    assert '@app.post("/v1/groups/{conversation_id}/messages", status_code=202)' in async_web
    assert 'scheduler.enqueue_group(conversation_id, event' in async_web
    assert 'self.repo.list_turn_events(group.id, source_event.turn_id)' in group_service
    assert 'self.repo.list_events(conversation_id, limit=180)' not in group_service
