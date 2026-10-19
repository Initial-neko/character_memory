from datetime import datetime, timedelta, timezone
import base64
import json

import pytest

from character_memory.rss_sources import ParsedFeedItem, RssRepository
from character_memory.space_store import SpaceRepository
from character_memory.storage.sqlite import SQLiteStore
from character_memory.world_activity import WorldPulseRepository, WorldPulseTopicDraft
from character_memory.space_feed import SpaceFeed


@pytest.fixture
def mixed(tmp_path):
    store = SQLiteStore(tmp_path / 'feed.db')
    posts, rss, pulse = SpaceRepository(store), RssRepository(store), WorldPulseRepository(store)
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    source = rss.create_source('https://example.com/rss', name='AI HOT')
    rss.upsert_items(source['id'], [ParsedFeedItem(
        key='a', title='一篇资讯', summary='来源原文摘要', content_text='文本',
        url='https://example.com/a', image_url='', published_at=now,
    )], fetched_at=now)
    post = posts.create_post('alice', '我有自己的想法', now)
    topic = pulse.upsert_topic(WorldPulseTopicDraft(title='一个热点', summary='聚合摘要'),
                               ['https://example.com/topic', 'javascript:alert(1)'], now)
    pulse.add_comment(topic['id'], 'alice', '不代表所有人都这么想', now)
    yield store, SpaceFeed(store), posts, rss, source, now, post
    store.close()


def test_mixed_feed_is_read_only_and_has_namespaced_identity(mixed):
    store, feed, _, _, _, _, _ = mixed
    before = store.conn.total_changes
    page = feed.page(limit=10)
    assert {row['kind'] for row in page['items']} == {'POST', 'RSS', 'TOPIC'}
    assert len({row['key'] for row in page['items']}) == 3
    assert store.conn.total_changes == before
    rss = next(row for row in page['items'] if row['kind'] == 'RSS')
    assert rss['source']['name'] == 'AI HOT'
    assert rss['source']['url'] == 'https://example.com/a'
    assert rss['content_scope'] == 'RSS_FEED_TEXT'
    assert 'character_id' not in rss
    topic = next(row for row in page['items'] if row['kind'] == 'TOPIC')
    assert topic['verification'] == 'UNVERIFIED_AGGREGATION'
    assert topic['source_urls'] == ['https://example.com/topic']
    assert topic['comments'][0]['character_id'] == 'alice'
    assert topic['comments'][0]['content'] == '不代表所有人都这么想'


def test_cursor_ties_filters_and_new_arrivals(mixed):
    _, feed, posts, _, source, now, _ = mixed
    first = feed.page(limit=1)
    assert first['has_more']
    posts.create_post('bob', '稍后新到', now + timedelta(hours=1))
    second = feed.page(limit=1, cursor=first['next_cursor'])
    third = feed.page(limit=1, cursor=second['next_cursor'])
    assert len({row['key'] for page in (first, second, third) for row in page['items']}) == 3
    assert not third['has_more']
    assert [row['kind'] for row in feed.page(kind='RSS', source_id=source['id'])['items']] == ['RSS']
    assert all(row['kind'] == 'POST' for row in feed.page(character_id='alice')['items'])
    with pytest.raises(ValueError):
        feed.page(cursor='bad')
    with pytest.raises(ValueError):
        feed.page(kind='WRONG')
    oversized = base64.urlsafe_b64encode(json.dumps([10**40, 'RSS', 1]).encode()).decode()
    with pytest.raises(ValueError):
        feed.page(cursor=oversized)


def test_author_filter_keeps_deferred_roles_out_of_the_post_stream(mixed):
    _, feed, posts, _, _, now, visible = mixed
    hidden = posts.create_post('hidden', '群聊里的临时角色', now + timedelta(minutes=1))
    page = feed.page(character_ids=['alice'])
    post_ids = [row['id'] for row in page['items'] if row['kind'] == 'POST']
    assert post_ids == [visible.id]
    assert hidden.id not in post_ids
    assert feed.page(kind='POST', character_ids=[])['items'] == []
    # RSS and topics have no character author, so the filter leaves them alone.
    assert {row['kind'] for row in page['items']} == {'POST', 'RSS', 'TOPIC'}


def test_cancelled_subscriptions_leave_space_but_keep_rss_history(mixed):
    _, feed, _, rss, source, _, _ = mixed
    rss.cancel_source(source['id'])
    assert not feed.page(kind='RSS')['items']
    assert rss.list_items(source_id=source['id'])


def test_unavailable_optional_tables_are_empty_not_fatal(tmp_path):
    store = SQLiteStore(tmp_path / 'minimal.db')
    SpaceRepository(store)
    assert SpaceFeed(store).page()['items'] == []
    store.close()
