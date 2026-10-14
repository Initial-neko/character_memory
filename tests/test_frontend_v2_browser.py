"""Presentation keeps the existing conversation and source boundaries."""
import os
import pytest
from test_life_conversation_browser import live_life
from test_life_conversation import isolated_app

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT') != '1', reason='explicit browser run')]


def test_shared_experiences_require_click_and_never_send(page, live_life):
    from playwright.sync_api import expect
    base, character, event, store, access = live_life
    page.goto(base + '/')
    page.locator(f'[data-character="{character}"]').click()
    shared = page.get_by_role('button', name='共同经历', exact=True)
    expect(shared).to_be_visible()
    expect(page.locator('#drawer')).not_to_have_class('drawer open')
    count = store.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    shared.click()
    expect(page.locator('#drawer')).to_have_class('drawer open')
    expect(page.locator('#drawerTitle')).to_have_text('共同经历')
    expect(page.locator('#drawerBody a')).to_have_attribute('href', f'/life?character={character}&view=relationships')
    assert store.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == count
    assert page.request.get(base + '/health').json()['runtime_loaded'] is False


def test_global_space_navigation_reuses_existing_feed(page, live_life):
    from playwright.sync_api import expect
    base, _, _, _, _ = live_life
    page.goto(base + '/')
    page.locator('.cm-global-nav').get_by_role('button', name='Space', exact=True).click()
    expect(page.locator('.space-shell')).to_be_visible()
    expect(page.locator('.cm-global-nav [data-cm-nav="space"]')).to_have_attribute('aria-current', 'page')
    page.locator('.cm-global-nav').get_by_role('button', name='聊天', exact=True).click()
    expect(page.locator('.space-shell')).to_be_hidden()
    expect(page.locator('#messageInput')).to_be_visible()


def test_world_discovery_keeps_chat_and_source_entry_separate(page, live_life):
    from playwright.sync_api import expect
    base, _, _, _, _ = live_life
    requests=[]
    page.on('request',lambda request: requests.append((request.method,request.url)))
    page.goto(base + '/')
    page.locator('.cm-global-nav').get_by_role('link', name='World', exact=True).click()
    expect(page.locator('.cm-world-page')).to_be_visible()
    expect(page.locator('.encounter-section')).to_be_visible()
    expect(page.locator('.encounter-empty')).not_to_contain_text('正在看看')
    assert any(method=='GET' and '/v1/encounters?' in url for method,url in requests)
    expect(page.locator('.cm-world-page').get_by_role('link', name='订阅阅读')).to_have_attribute('href', '/sources')
    expect(page.locator('#messageInput')).to_be_hidden()
    page.locator('.cm-global-nav').get_by_role('button', name='聊天', exact=True).click()
    expect(page.locator('.cm-world-page')).to_be_hidden()
    expect(page.locator('#messageInput')).to_be_visible()


def test_narrow_chat_controls_do_not_overflow(page, live_life):
    from playwright.sync_api import expect
    base, _, _, _, _ = live_life
    page.set_viewport_size({'width':390,'height':844})
    page.goto(base + '/')
    page.wait_for_function("document.body.dataset.cmReady === 'true'")
    expect(page.locator('#sendButton')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate("Array.from(document.querySelectorAll('body *')).filter(n => n.getBoundingClientRect().right > innerWidth && n.getClientRects().length && getComputedStyle(n).position !== 'fixed').slice(0,10).map(n => ({tag:n.tagName,cls:n.className,right:n.getBoundingClientRect().right}))")
    for selector in ('#sendButton', '.cm-shared-button', '#voiceCallButton', '.cm-global-nav'):
        box=page.locator(selector).bounding_box()
        assert box and box['x'] >= 0 and box['x']+box['width'] <= 390, selector


def test_latest_navigation_overrides_initial_profile_during_startup(page, live_life):
    from playwright.sync_api import expect
    base, _, _, _, _ = live_life
    held=[]
    page.route('**/v1/characters',lambda route:held.append(route))
    page.goto(base+'/?view=profile',wait_until='domcontentloaded')
    page.locator('.cm-global-nav').get_by_role('button',name='Space',exact=True).click()
    assert held
    held[0].continue_()
    expect(page.locator('.space-shell')).to_be_visible()
    expect(page.locator('.profile-v2-shell')).to_be_hidden()


def test_navigation_survives_refresh_and_browser_back(page, live_life):
    from playwright.sync_api import expect
    base, _, _, _, _ = live_life
    page.goto(base+'/')
    page.wait_for_function("document.body.dataset.cmReady === 'true'")
    page.locator('.cm-global-nav').get_by_role('link',name='World',exact=True).click()
    expect(page).to_have_url(base+'/?view=world')
    page.reload()
    expect(page.locator('.cm-world-page')).to_be_visible()
    page.locator('.cm-global-nav').get_by_role('link',name='人物',exact=True).click()
    expect(page).to_have_url(base+'/?view=profile')
    page.go_back()
    expect(page.locator('.cm-world-page')).to_be_visible()
    expect(page.locator('.profile-v2-shell')).to_be_hidden()
    page.locator('.cm-global-nav').get_by_role('button',name='聊天',exact=True).click()
    expect(page).to_have_url(base+'/')
    page.reload()
    expect(page.locator('#messageInput')).to_be_visible()


def test_space_experience_rail_fetches_only_after_expansion(page, live_life):
    from playwright.sync_api import expect
    base, character, _, store, access = live_life
    requests=[]
    page.on('request',lambda request: requests.append(request.url))
    page.set_viewport_size({'width':1586,'height':992})
    page.goto(base+'/')
    page.wait_for_function("document.body.dataset.cmReady === 'true'")
    page.locator(f'[data-character="{character}"]').click()
    page.wait_for_function('id=>window.CM.state.characterId===id',arg=character)
    page.locator('.cm-global-nav').get_by_role('button',name='Space',exact=True).click()
    details=page.locator('[data-rail-experiences]')
    expect(details).to_be_visible()
    assert details.get_attribute('open') is None
    assert not any('/life?view=relationships' in url for url in requests)
    count=store.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    details.locator('summary').click()
    expect(details.locator('[data-rail-records]')).to_contain_text('浏览器原始事实')
    assert any('/life?view=relationships' in url for url in requests)
    assert store.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]==count
    assert page.evaluate('window.FORGED') is None
    assert page.request.get(base+'/health').json()['runtime_loaded'] is False
