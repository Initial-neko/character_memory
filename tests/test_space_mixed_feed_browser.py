"""Isolated mixed-content browser flow, with no paid calls or formal writes."""
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from character_memory.rss_sources import RssRepository, ParsedFeedItem
from character_memory.storage.sqlite import SQLiteStore
from character_memory.world_activity import WorldPulseRepository, WorldPulseTopicDraft
import test_space_reconciliation_playwright as server_module

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT') != '1', reason='explicit browser run')]


@pytest.fixture
def mixed_server(tmp_path_factory, monkeypatch):
    captured = {}
    popen = subprocess.Popen
    def capture(*args, **kwargs):
        env = kwargs.get('env') or {}
        if 'CHARACTER_MEMORY_E2E_DB' in env:
            captured.update(env)
        return popen(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(server_module.subprocess, 'Popen', capture)
        fixture = server_module.space_server.__wrapped__(tmp_path_factory)
        base = next(fixture)
    store = SQLiteStore(captured['CHARACTER_MEMORY_E2E_DB'])
    try:
        rss = RssRepository(store)
        source = rss.create_source('https://example.com/feed', name='AI HOT · 隔离示例')
        other = rss.create_source('https://example.org/feed', name='另一订阅')
        now = datetime.now(timezone.utc)
        for i in range(14):
            rss.upsert_items(source['id'] if i % 2 else other['id'], [ParsedFeedItem(
                key=str(i), title=f'信息 {i} · Feed 内容', summary='这是来源提供的摘要，不是角色的阅读经历。',
                content_text='正文只来自 Feed，不代表完整原文。', url=f'https://example.com/{i}', image_url='', published_at=now,
            )], fetched_at=now)
        pulse = WorldPulseRepository(store)
        topic = pulse.upsert_topic(WorldPulseTopicDraft(title='隔离演示话题', summary='此为隔离测试，不是实时新闻'),
                                   ['https://example.com/topic'], now,
                                   evidence=[{'kind':'OPINION', 'quote':'这是有归属的外界观点', 'url':'https://example.com/topic', 'author':'示例作者', 'published_at':'', 'verification':'EXACT_FETCHED_EXCERPT'}])
        pulse.add_comment(topic['id'], 'rin', '我只对这个角度感兴趣。', now)
        yield base, source['id']
    finally:
        store.close()
        fixture.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_mixed_tabs_sources_links_and_character_detail(page, mixed_server, width):
    from playwright.sync_api import expect
    base, source_id = mixed_server
    page.set_viewport_size({'width':width, 'height':960})
    created = page.request.post(f'{base}/v1/space/posts', data={'character_id':'rin', 'content':'角色自发的一句话。'}).json()['post']
    page.goto(f'{base}/?view=space')
    expect(page.locator('[data-space-entry]').first).to_be_visible()
    page.locator('[data-space-page-action="RSS"]').click()
    page.locator('.space-source-filter select').select_option(str(source_id))
    expect(page.locator('.space-source-name').first).to_have_text('AI HOT · 隔离示例')
    assert page.locator('.space-source-filter').bounding_box()['y'] < page.locator('.space-feed').bounding_box()['y']
    expect(page.locator('[data-space-post]')).to_have_count(0)
    assert page.locator('.space-source-links a').first.get_attribute('href').startswith('/sources?item=')
    article_href = page.locator('.space-source-links a').first.get_attribute('href')
    page.locator('[data-space-page-action="TOPIC"]').click()
    expect(page.locator('.space-external-card h2')).to_have_text('隔离演示话题')
    page.get_by_text('来源节选与外界观点 · 1', exact=True).click()
    expect(page.locator('.space-outside-excerpt')).to_contain_text('示例作者')
    page.get_by_text('角色的回应 · 1', exact=True).click()
    expect(page.locator('.space-topic-take')).to_contain_text('我只对这个角度感兴趣')
    assert page.locator('.space-source-links a').first.get_attribute('href') == 'https://example.com/topic'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = os.getenv('MIXED_FEED_SCREENSHOTS')
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        page.evaluate('window.scrollTo(0,0)')
        page.screenshot(path=str(Path(output) / f'isolated-topic-{width}.png'), full_page=True)
    page.locator('[data-space-page-action="POST"]').click()
    page.locator(f'[data-space-post-open="{created["id"]}"]').click()
    expect(page.locator('.space-post-detail-text-only')).to_be_visible()
    expect(page.locator('.space-source-filter')).to_be_hidden()
    page.locator('[data-space-detail-back]').click()
    expect(page.locator('[data-space-post]').first).to_be_visible()
    page.goto(base + article_href)
    expect(page.locator('#articleBody')).to_contain_text('正文只来自 Feed，不代表完整原文。')
    expect(page.locator('.article-original a')).to_have_attribute('href', 'https://example.com/13')
