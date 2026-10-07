from datetime import datetime, timezone

import pytest
from dataclasses import replace

from character_memory.rss_sources import RssRepository, parse_feed
from character_memory.storage.sqlite import SQLiteStore


def rss_content(html):
    return f'''<rss version="2.0"><channel><title>Content</title><item><guid>same</guid>
    <title>文章</title><link>https://example.com/posts/one</link>
    <description><![CDATA[{html}]]></description></item></channel></rss>'''


def test_content_keeps_paragraphs_lists_and_multiple_images():
    item = parse_feed(rss_content('''<h2>小标题</h2><p>第一段 <strong>重点</strong></p>
      <p>第二段<br>另一行</p><ul><li>项目一</li><li>项目二</li></ul>
      <img src="/one.png" alt="第一张"><p>图之间的内容</p><img data-src="/two.jpg" alt="第二张">''')).items[0]
    assert "第一段 重点\n" in item.content_text
    assert "第二段\n另一行" in item.content_text
    assert "项目一\n项目二" in item.content_text
    assert '<h2>小标题</h2>' in item.content_html
    assert item.content_html.count('<img ') == 2
    assert 'https://example.com/one.png' in item.content_html
    assert 'https://example.com/two.jpg' in item.content_html
    assert item.image_url == 'https://example.com/one.png'


def test_feed_markup_is_sanitized_before_storage_and_display():
    item = parse_feed(rss_content('''<p onclick="evil()">正文</p><script>evil()</script>
      <style>body{display:none}</style><iframe src="https://evil.example"></iframe>
      <a href="javascript:evil()">危险链接</a><img src="data:image/svg+xml,bad" onerror="evil()">
      <img src="/safe.webp" onerror="evil()"><a href="/reference">参考</a>''')).items[0]
    assert 'evil' not in item.content_html
    assert '<script' not in item.content_html
    assert '<iframe' not in item.content_html
    assert 'onclick' not in item.content_html
    assert 'onerror' not in item.content_html
    assert 'javascript:' not in item.content_html
    assert 'data:' not in item.content_html
    assert 'https://example.com/reference' in item.content_html
    assert 'evil' not in item.content_text
    assert item.image_url == 'https://example.com/safe.webp'


def test_atom_xhtml_retains_element_markup_and_xml_base():
    xml = '''<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://example.com/blog/">
      <title>Atom</title><entry><id>xhtml-one</id><title>XHTML</title><link href="entry/"/>
      <content type="xhtml" xml:base="https://cdn.example.com/articles/">
        <div xmlns="http://www.w3.org/1999/xhtml"><p>第一段</p><p>第二段</p><img src="one.png" /></div>
      </content></entry></feed>'''
    item = parse_feed(xml, feed_url='https://example.com/feed.xml').items[0]
    assert item.url == 'https://example.com/blog/entry/'
    assert '<p>第一段</p>' in item.content_html
    assert '<p>第二段</p>' in item.content_html
    assert item.image_url == 'https://cdn.example.com/articles/one.png'


def test_refresh_repairs_existing_articles_without_changing_identity(tmp_path):
    store = SQLiteStore(tmp_path / 'rich.db')
    try:
        repo = RssRepository(store)
        source = repo.create_source('https://example.com/rss')
        first = parse_feed(rss_content('<p>原来</p>')).items
        fetched = datetime(2026, 10, 7, tzinfo=timezone.utc)
        assert repo.upsert_items(source['id'], first, fetched_at=fetched) == 1
        original = repo.list_items()[0]
        updated = parse_feed(rss_content('<p>第一段</p><p>第二段</p><img src="/new.jpg">')).items
        assert repo.upsert_items(source['id'], updated, fetched_at=datetime(2026, 10, 8, tzinfo=timezone.utc)) == 0
        repaired = repo.get_item(original['id'])
        assert repaired['id'] == original['id']
        assert repaired['fetched_at'] == original['fetched_at']
        assert '第二段' in repaired['content_html']
        assert '\n' in repaired['content_text']
        assert repaired['image_url'] == 'https://example.com/new.jpg'
    finally:
        store.close()


def test_old_schema_migration_retains_rows_and_is_repeatable(tmp_path):
    store = SQLiteStore(tmp_path / 'legacy.db')
    try:
        repo = RssRepository(store)
        source = repo.create_source('https://example.com/feed')
        repo.upsert_items(source['id'], parse_feed(rss_content('旧内容')).items, fetched_at=datetime.now(timezone.utc))
        # Reconstruct exactly the V1 shape while keeping its existing rows.
        columns = [row['name'] for row in store.conn.execute('PRAGMA table_info(rss_items)')]
        if 'content_html' in columns:
            store.conn.execute('ALTER TABLE rss_items DROP COLUMN content_html')
            store.conn.execute("DELETE FROM schema_migrations WHERE name='rss/002-rich-content'")
            store.conn.commit()
        migrated = RssRepository(store)
        assert migrated.get_item(1)['content_html'] == ''
        assert len(migrated.list_items()) == 1
        assert RssRepository(store).get_item(1)['content_text'] == '旧内容'
    finally:
        store.close()


def test_content_refresh_keeps_pagination_sort_keys_stable(tmp_path):
    store = SQLiteStore(tmp_path / 'pagination.db')
    try:
        repo = RssRepository(store)
        source = repo.create_source('https://example.com/feed')
        template = parse_feed(rss_content('<p>正文</p>')).items[0]
        entries = [replace(template, key=f'item-{i}', published_at=datetime(2026, 10, 7-i, tzinfo=timezone.utc)) for i in range(3)]
        repo.upsert_items(source['id'], entries, fetched_at=datetime.now(timezone.utc))
        first = repo.list_items(limit=2)
        repo.upsert_items(source['id'], [replace(entries[1], published_at=datetime(2026, 10, 8, tzinfo=timezone.utc), content_html='<p>修复正文</p>')], fetched_at=datetime.now(timezone.utc))
        second = repo.list_items(before_id=first[-1]['id'])
        assert [item['id'] for item in first + second] == [1, 2, 3]
        assert repo.get_item(2)['published_at'] == first[-1]['published_at']
        assert repo.get_item(2)['content_html'] == '<p>修复正文</p>'
    finally:
        store.close()


def test_item_image_route_only_fetches_recorded_public_raster_images(tmp_path):
    import base64
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from character_memory.rss_sources import RssService
    from character_memory.rss_web import attach_rss_routes
    from pathlib import Path

    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jCwkAAAAASUVORK5CYII=')
    store = SQLiteStore(tmp_path / 'images.db')
    repo = RssRepository(store)
    source = repo.create_source('https://93.184.216.34/feed')
    entries = parse_feed(rss_content('<p>图</p><img src="https://93.184.216.34/photo.png"><img src="http://127.0.0.1/private.png"><img src="https://93.184.216.34/not-image.png">')).items
    repo.upsert_items(source['id'], entries, fetched_at=datetime.now(timezone.utc))
    service = RssService(repo, client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b'<script>bad</script>' if request.url.path == '/not-image.png' else png, headers={'content-type':'image/png'}),
    )))
    app = FastAPI()
    attach_rss_routes(app, Path('.'), repo, service)
    try:
        with TestClient(app) as client:
            response = client.get('/v1/rss/items/1/image', params={'url':'https://93.184.216.34/photo.png'})
            assert response.status_code == 200
            assert response.content == png
            assert response.headers['content-type'] == 'image/png'
            assert response.headers['x-content-type-options'] == 'nosniff'
            assert client.get('/v1/rss/items/1/image', params={'url':'https://93.184.216.34/unrecorded.png'}).status_code == 404
            assert client.get('/v1/rss/items/1/image', params={'url':'http://127.0.0.1/private.png'}).status_code == 400
            assert client.get('/v1/rss/items/1/image', params={'url':'https://93.184.216.34/not-image.png'}).status_code == 400
    finally:
        store.close()
