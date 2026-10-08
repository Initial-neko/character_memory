from datetime import timedelta

import pytest

from character_memory.domain.models import Event, EventType
from character_memory.rss_sources import ParsedFeedItem, RssRepository
from character_memory.runtime.context import render_observed_experiences
from character_memory.world_activity import WorldActivityService
from test_character_architecture_baseline import baseline
from test_character_lifecycle import rss_reading_fixture
from test_world_activity import NOW


@pytest.mark.parametrize("question", [
    "你有没有读过琥珀镇温室日志？",
    "刚才你有没有实际读过琥珀镇温室日志？请只说实际读到的内容和你的看法。",
    "你还记得琥珀镇温室日志吗？",
    "你是否看过琥珀镇温室日志呢？",
])
def test_chinese_question_recalls_real_read_title_after_recent_window(baseline, monkeypatch, question):
    store, access, model, _, repo, _, _, item_id = rss_reading_fixture(baseline, monkeypatch)
    source = RssRepository(store).get_item(item_id)["source_id"]
    RssRepository(store).upsert_items(source, [ParsedFeedItem("governance", "[验收素材]琥珀镇温室日志", "excerpt",
        "A source text on plant observation", "https://example.com/garden", "", NOW)], fetched_at=NOW)
    result = WorldActivityService(access, repo).browse_character("c00", now=NOW, opportunity_id="chinese:title")
    event_id = result["items"][0]["source_event_id"]
    for i in range(30):
        store.append_event(Event(character_id="c00", event_type=EventType.USER_MESSAGE,
            event_time=NOW + timedelta(minutes=i+1), content="unrelated conversation"))
    at = NOW + timedelta(hours=1)
    assert [e.id for e in store.recall_observed_events("c00", question, at=at)] == [event_id]
    assert store.recall_observed_events("c01", question, at=at) == []
    assert store.recall_observed_events("c00", question, at=at, projection="PUBLIC") == []
    assert store.recall_observed_events("c00", question, at=NOW-timedelta(hours=1)) == []
    assert store.recall_observed_events("c00", "你有没有读过不存在的月光日报？", at=at) == []
    model.calls.clear()
    access.require_bundle().runtimes["c00"].handle(Event(character_id="c00", event_type=EventType.USER_MESSAGE,
        event_time=at, content=question))
    assert len(model.calls) == 1
    prompt = model.calls[0][2]
    assert "琥珀镇温室日志" in prompt and "RSS_FEED_TEXT" in prompt
    assert "An RSS excerpt discusses agent memory governance." in prompt
    assert f"event_id={event_id} " in prompt


def test_observed_render_provenance_summary_stays_inside_existing_budget():
    events = [Event(id=i+1, character_id="a", event_type=EventType.WORLD_OBSERVATION, event_time=NOW,
        content="opinion"*500, metadata={"sources": ["https://example.com/"+"x"*600],
            "world_summary": "summary"*500, "rss_reading": {"title": "title"*500, "content_scope": "RSS_FEED_TEXT"}}) for i in range(6)]
    rendered = render_observed_experiences(events)
    assert len(rendered) <= 4000
    assert "RSS_FEED_TEXT" in rendered and "summary" in rendered and "opinion" in rendered
    assert "event_id=5 " not in rendered


@pytest.mark.parametrize("reading", [None, [], "malformed", {"title": ["not a title"], "content_scope": []}])
def test_malformed_optional_reading_metadata_is_not_fabricated(reading):
    event = Event(id=1, character_id="a", event_type=EventType.WORLD_OBSERVATION, event_time=NOW,
        content="original observation", metadata={"rss_reading": reading, "world_summary": []})
    rendered = render_observed_experiences([event])
    assert "original observation" in rendered and "not a title" not in rendered
