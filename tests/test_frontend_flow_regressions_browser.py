"""User journeys reproduced during visual acceptance; isolated providers only."""
import os

import pytest

from test_rss_browser import rss_server
from test_life_conversation import isolated_app
from test_life_conversation_browser import live_life
from test_profile_v2_browser import profile_app, prepare_profile

pytestmark = [pytest.mark.browser, pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT") != "1", reason="explicit isolated browser validation")]


def test_rss_deep_link_outside_default_page_survives_list_load(page, rss_server):
    from playwright.sync_api import expect
    page.goto(rss_server + "/sources?item=39")
    expect(page.locator("#articleBody h1")).to_have_text("昨天的 AI 新闻")
    expect(page.locator("#feedStatus")).to_contain_text("已显示")
    expect(page.locator("#articleBody h1")).to_have_text("昨天的 AI 新闻")
    page.reload()
    expect(page.locator("#articleBody h1")).to_have_text("昨天的 AI 新闻")
    page.locator('[data-view="sources"]').click()
    expect(page.locator("#articlePanel")).to_be_hidden()
    page.set_viewport_size({"width": 390, "height": 844})
    assert not page.locator('.rss-main').evaluate('node=>node.inert')
    page.get_by_role('button', name='＋ 添加订阅', exact=True).click()
    expect(page.get_by_role('dialog', name='添加 RSS 订阅')).to_be_visible()
    page.get_by_role('button', name='取消', exact=True).click()
    page.set_viewport_size({"width": 1440, "height": 960})
    page.locator('[data-view="feed"]').click()
    expect(page.locator("#articlePanel")).to_be_visible()


def test_mobile_chat_keeps_identity_and_group_experience_context(page, live_life):
    from playwright.sync_api import expect
    base, character, _, _, access = live_life
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base + "/")
    page.wait_for_function("document.body.dataset.cmReady === 'true'")
    heading = page.locator(".character-heading-copy")
    assert heading.bounding_box()["width"] >= 180
    members = [p['id'] for p in access.character_profiles()]
    response = page.request.post(base + "/v1/groups", data={"name": "上下文验收群", "member_ids": members})
    assert response.ok
    page.set_viewport_size({"width": 1440, "height": 960})
    page.reload()
    page.locator('[data-group]').first.click()
    page.get_by_role("button", name="共同经历", exact=True).click()
    expect(page.locator("#drawerBody")).to_contain_text("上下文验收群")
    expect(page.locator("#drawerBody a").first).to_have_attribute("href", f"/life?character={character}&view=relationships")


def test_profile_post_links_to_real_detail(page, profile_app):
    from playwright.sync_api import expect
    base, character, _, store, access = profile_app
    from datetime import datetime, timezone
    post = access.space_repository.create_post(character_id=character, content="可跳转的动态", now=datetime.now(timezone.utc))
    prepare_profile(page, base, character)
    page.locator('.cm-profile-link').click()
    card = page.locator(f'[data-profile-post="{post.id}"]')
    expect(card.get_by_role('link', name='查看动态')).to_have_attribute('href', f'/?view=space&space_post={post.id}')
    card.locator('.profile-v2-card-content').click()
    expect(page.get_by_role('region', name='动态详情')).to_contain_text('可跳转的动态')


def test_mobile_life_record_opens_detail_near_viewport(page, live_life):
    from playwright.sync_api import expect
    base, character, _, _, _ = live_life
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base + f"/life?character={character}")
    page.locator('.event-card').first.click()
    details = page.locator('#details')
    expect(details).to_be_visible()
    assert 0 <= details.bounding_box()['y'] < 400
    details.get_by_role('button', name='返回生活记录').click()
    expect(page.locator('.event-card').first).to_be_focused()


def test_memory_link_reads_exact_owned_memory_and_post_author_opens_profile(page, profile_app, isolated_app):
    from datetime import datetime, timezone
    from character_memory.domain.models import Memory
    from playwright.sync_api import expect
    from character_memory.memory_web import attach_memory_routes
    attach_memory_routes(isolated_app)
    base, character, event, store, access = profile_app
    name = next(p['name'] for p in access.character_profiles() if p['id'] == character)
    memory = store.add_memory(Memory(character_id=character, content='关联记忆验收',
        memory_type='SHARED', event_time=datetime.now(timezone.utc), importance=0.6,
        source_event_id=event.id, embedding=[0.0]))
    before = store.conn.total_changes
    page.goto(base + f'/?memory_character={character}&memory_id={memory.id}')
    expect(page.locator('#drawerBody')).to_contain_text('关联记忆验收')
    expect(page.locator('#drawerTitle')).to_have_text('关联记忆')
    assert 'memory_character=' not in page.url
    assert store.conn.total_changes == before
    post = access.space_repository.create_post(character_id=character,
        content='作者导航验收', now=datetime.now(timezone.utc))
    page.goto(base + f'/?view=space&space_post={post.id}')
    detail = page.get_by_role('region', name='动态详情')
    detail.get_by_role('link', name=f'查看 {name} 的人物主页').click()
    expect(page.get_by_role('region', name='人物主页')).to_be_visible()
    expect(page.locator('[data-profile-name]')).to_have_text(name)
