"""Presentation keeps the existing conversation and source boundaries."""
import os
import pytest
from test_life_conversation_browser import live_life
from test_life_conversation import isolated_app

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT') != '1', reason='explicit browser run')]


def test_desktop_chat_has_compact_readable_sizes(page, live_life):
    from playwright.sync_api import expect
    base, character, _, _, _ = live_life
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.goto(base + '/')
    page.wait_for_function("document.body.dataset.cmReady === 'true'")
    page.locator(f'[data-character="{character}"]').click()
    expect(page.locator('.composer')).to_be_visible()
    assert page.locator('.composer').bounding_box()['height'] <= 115
    assert page.locator('.topbar').bounding_box()['height'] <= 95
    assert page.locator('.sidebar').bounding_box()['width'] == 272


def test_late_chat_image_keeps_latest_message_above_composer(page, live_life):
    from datetime import datetime, timedelta, timezone
    import io
    from PIL import Image
    from playwright.sync_api import expect
    from character_memory.domain.models import Event, EventType

    base, character, _, store, access = live_life
    picture = io.BytesIO()
    Image.new('RGB', (400, 480), '#c6b5ef').save(picture, format='PNG')
    now = datetime.now(timezone.utc)
    asset = access.media_storage.save_bytes(character_id=character, original_name='test.png',
        payload=picture.getvalue(), created_at=now, source='GENERATED_TEST')
    store.add_media_asset(asset)
    for index in range(10):
        store.append_event(Event(character_id=character, event_type=EventType.CHARACTER_MESSAGE,
            event_time=now + timedelta(seconds=index), content=f'旧消息 {index}',
            metadata={'action':'MESSAGE', 'conversation_id':character+':default'}))
    store.append_event(Event(character_id=character, event_type=EventType.CHARACTER_MESSAGE,
        event_time=now+timedelta(seconds=11), content='图片',
        metadata={'action':'IMAGE','media_id':asset.id,'conversation_id':character+':default'}))
    store.append_event(Event(character_id=character, event_type=EventType.CHARACTER_MESSAGE,
        event_time=now+timedelta(seconds=12), content='图片之后的最新消息',
        metadata={'action':'MESSAGE','conversation_id':character+':default'}))
    held = []
    page.route('**/v1/media/'+asset.id, lambda route: held.append(route))
    with page.expect_request(lambda request: request.url.endswith('/v1/media/'+asset.id)):
        page.goto(base+'/')
        page.locator(f'[data-character="{character}"]').click()
        expect(page.get_by_text('图片之后的最新消息', exact=True)).to_be_visible()
    page.evaluate('() => new Promise(requestAnimationFrame)')
    assert held
    held.pop().fulfill(content_type='image/png', body=picture.getvalue())
    page.wait_for_function("""() => {
      const image=document.querySelector('.chat .image-bubble img');
      const last=document.querySelector('.message-row:last-child');
      const composer=document.querySelector('.composer-wrap');
      return image?.complete && image.naturalWidth>0 && last.getBoundingClientRect().bottom<=composer.getBoundingClientRect().top;
    }""", timeout=5000)


def test_chat_broken_image_has_readable_fallback(page, live_life):
    from playwright.sync_api import expect
    base, _, _, _, _ = live_life
    page.goto(base+'/')
    # Exercise the same DOM structure supplied by the real shared renderer.
    page.evaluate("""() => {
      const host=document.createElement('div'); host.className='image-bubble';
      const image=document.createElement('img'); image.src='/missing-image-for-browser-test.png';
      host.append(image); document.querySelector('.chat').append(host);
    }""")
    expect(page.get_by_text('图片暂时无法加载', exact=True)).to_be_visible()
    expect(page.locator('.image-bubble img')).to_be_hidden()


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
    selected = page.locator('.character-item.active').get_attribute('data-character')
    expect(page).to_have_url(base+f'/?view=profile&character={selected}')
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
