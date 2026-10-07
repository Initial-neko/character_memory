from datetime import datetime, timezone

from character_memory.rss_sources import ParsedFeedItem, RssRepository, parse_feed
from character_memory.storage.sqlite import SQLiteStore


RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example RSS</title>
    <link>https://example.com/</link>
    <item>
      <guid>post-1</guid>
      <title>第一篇</title>
      <link>https://example.com/1</link>
      <description><![CDATA[<p>摘要内容</p><img src="/cover.jpg">]]></description>
      <pubDate>Wed, 07 Oct 2026 08:00:00 +0800</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <link href="https://example.org/" rel="alternate"/>
  <entry>
    <id>tag:example.org,2026:1</id>
    <title>Atom entry</title>
    <link href="https://example.org/posts/1"/>
    <summary>hello atom</summary>
    <updated>2026-10-07T09:30:00+08:00</updated>
  </entry>
</feed>
"""


def test_parse_rss_and_atom():
    rss = parse_feed(RSS_SAMPLE, feed_url="https://example.com/feed.xml")
    assert rss.title == "Example RSS"
    assert rss.site_url == "https://example.com/"
    assert len(rss.items) == 1
    assert rss.items[0].title == "第一篇"
    assert rss.items[0].summary == "摘要内容"
    assert rss.items[0].image_url == "https://example.com/cover.jpg"

    atom = parse_feed(ATOM_SAMPLE, feed_url="https://example.org/feed.xml")
    assert atom.title == "Example Atom"
    assert atom.site_url == "https://example.org/"
    assert atom.items[0].url == "https://example.org/posts/1"
    assert atom.items[0].summary == "hello atom"


def test_repository_deduplicates_items(tmp_path):
    store = SQLiteStore(tmp_path / "rss.db")
    try:
        repository = RssRepository(store, seed_defaults=False)
        source = repository.create_source("https://example.com/feed.xml", name="Example")
        item = ParsedFeedItem(
            key="same-key",
            title="title",
            summary="summary",
            content_text="content",
            url="https://example.com/1",
            image_url="",
            published_at=datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
        assert repository.upsert_items(source["id"], [item], fetched_at=now) == 1
        assert repository.upsert_items(source["id"], [item], fetched_at=now) == 0
        items = repository.list_items(source_id=source["id"])
        assert len(items) == 1
        assert items[0]["title"] == "title"
    finally:
        store.close()


def test_source_enable_disable(tmp_path):
    store = SQLiteStore(tmp_path / "rss.db")
    try:
        repository = RssRepository(store, seed_defaults=False)
        source = repository.create_source("https://example.com/feed.xml")
        assert source["enabled"] is True
        disabled = repository.set_enabled(source["id"], False)
        assert disabled["enabled"] is False
    finally:
        store.close()
