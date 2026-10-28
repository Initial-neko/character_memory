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
    assert page.locator('.sidebar').bounding_box()['width'] == 236


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
        store.append_message(Event(character_id=character, event_type=EventType.CHARACTER_MESSAGE,
            event_time=now + timedelta(seconds=index), content=f'旧消息 {index}',
            metadata={'action':'MESSAGE', 'conversation_id':character+':default'}))
    store.append_message(Event(character_id=character, event_type=EventType.CHARACTER_MESSAGE,
        event_time=now+timedelta(seconds=11), content='图片',
        metadata={'action':'IMAGE','media_id':asset.id,'conversation_id':character+':default'}))
    store.append_message(Event(character_id=character, event_type=EventType.CHARACTER_MESSAGE,
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


@pytest.mark.parametrize('width',[390,1440])
def test_character_menu_survives_summary_refresh_without_changing_identity(page,live_life,width):
    from playwright.sync_api import expect
    base,character,_,_,_=live_life
    page.set_viewport_size({'width':width,'height':844 if width==390 else 960})
    page.goto(base+'/')
    page.wait_for_function("document.body.dataset.cmReady==='true'")
    page.evaluate('async()=>{await CM.features.unread.refresh()}')
    if width==390:
        page.locator('#sidebarMobileButton').click()
    row=page.locator('[data-character-row="'+character+'"]')
    row.locator('[data-character-more]').click()
    menu=page.locator('[data-character-menu="'+character+'"]')
    action=menu.locator('[data-character-archive]')
    expect(menu).to_be_visible()
    action.focus()
    page.evaluate('async()=>{await CM.features.unread.refresh()}')
    expect(menu).to_be_visible()
    expect(action).to_be_focused()
    assert action.get_attribute('data-character-archive')==character
    from test_mobile_views_playwright import _record_visual
    _record_visual(page,f'character-menu-after-refresh-{width}',('src/character_memory/web/app.js','src/character_memory/web/character_archive.js'))
    page.keyboard.press('Escape')
    expect(menu).to_be_hidden()
    if width==390:
        page.locator('#sidebarMobileButton').click()
    row.locator('[data-character-more]').click()
    page.evaluate('(id)=>{CM.state.characters=CM.state.characters.filter(p=>p.id!==id);CM.renderCharacterList()}',character)
    expect(menu).to_have_count(0)
    expect(page.locator('[data-character-menu]:not(.hidden)')).to_have_count(0)


class StaleHistoryRender(AssertionError):
    """Only the confirmed cross-person repaint is an expected failure."""


@pytest.mark.parametrize("width", [1440, 390])
def test_older_direct_history_cannot_repaint_after_character_switch(page, live_life, width):
    from datetime import datetime, timedelta, timezone
    import json
    from pathlib import Path
    import subprocess
    from urllib.parse import parse_qs, urlsplit
    from playwright.sync_api import expect
    from character_memory.domain.models import Event, EventType

    base, first, _, store, access = live_life
    second = next(p["id"] for p in access.character_profiles() if p["id"] != first)
    now = datetime.now(timezone.utc)
    for index in range(60):
        store.append_message(Event(character_id=first, event_type=EventType.USER_MESSAGE,
            event_time=now + timedelta(seconds=index), content=f"History-A-{index:02d}"))
    store.append_message(Event(character_id=second, event_type=EventType.USER_MESSAGE,
        event_time=now, content="History-B-current"))
    page.set_viewport_size({"width": width, "height": 960 if width > 500 else 844})
    page.goto(base + "/")
    page.wait_for_function("document.body.dataset.cmReady === 'true'")
    page.evaluate("async id => { await CM.switchCharacter(id); }", first)
    expect(page.locator("#chat")).to_contain_text("History-A-59")
    assert page.evaluate("CM.state.directHistory.hasMore") is True
    page.evaluate("""() => {
      window.historyRenderEvents = [];
      const original = CM.renderHistory;
      CM.renderHistory = (...args) => {
        const result = original(...args);
        window.historyRenderEvents.push({character: CM.state.characterId,
          contents: args[0].map(message => message.content),
          rendered_text: document.querySelector('#chat').innerText});
        return result;
      };
    }""")
    held = []

    def hold_older(route):
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("character_id") == [first] and "before_id" in query:
            held.append(route)
            page.evaluate("window.olderHistoryHeld = true")
        else:
            route.continue_()

    page.route("**/v1/chat/history-page?*", hold_older)
    with page.expect_request(lambda request: "before_id=" in request.url):
        page.evaluate("() => { window.pendingOlderHistory = CM.loadOlderDirectHistory(); }")
    page.wait_for_function("window.olderHistoryHeld === true")
    assert len(held) == 1
    page.evaluate("async id => { await CM.switchCharacter(id); }", second)
    expect(page.locator("#chat")).to_contain_text("History-B-current")
    held.pop().continue_()
    page.evaluate("async () => { await window.pendingOlderHistory; }")
    assert page.evaluate("CM.state.characterId") == second
    assert page.evaluate("CM.state.directHistory.messages.some(m => m.content === 'History-B-current')") is True

    # Only synthetic messages from this test-owned database enter the evidence.
    root = Path(__file__).resolve().parents[1]
    evidence = Path(os.getenv("CORE_VISUAL_EVIDENCE_DIR", root / "artifacts/core-visual-evidence"))
    evidence.mkdir(parents=True, exist_ok=True)
    stem = f"history-switch-race-{width}"
    page.screenshot(path=str(evidence / f"{stem}.png"), full_page=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    (evidence / f"{stem}.json").write_text(json.dumps({
        "commit": commit, "viewport": page.viewport_size,
        "timestamp": datetime.now(timezone.utc).isoformat(), "scenario": "late older page after switch",
        "state_character": second, "state_messages": [m["content"] for m in page.evaluate("CM.state.directHistory.messages")],
        "rendered_text": page.locator("#chat").inner_text(), "render_events": page.evaluate("window.historyRenderEvents"), "test_fixture_only": True,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    wrong_renders = [event for event in page.evaluate("window.historyRenderEvents")
        if event["character"] == second and any(content.startswith("History-A-") for content in event["contents"])]
    if wrong_renders:
        raise StaleHistoryRender("Late older-history cleanup rendered the previous person under the current identity")
    expect(page.locator("#chat")).to_contain_text("History-B-current", timeout=1000)
    expect(page.locator("#chat")).not_to_contain_text("History-A-")


def test_late_direct_history_cannot_announce_a_previous_switch(page, live_life):
    """A switch whose history was overtaken must not connect or emit for its id."""

    from urllib.parse import parse_qs, urlsplit
    from playwright.sync_api import expect

    base, first, _, _, access = live_life
    second = next(p["id"] for p in access.character_profiles() if p["id"] != first)
    page.goto(base + "/")
    page.wait_for_function("document.body.dataset.cmReady === 'true'")
    page.evaluate("async id => { await CM.switchCharacter(id); }", first)
    expect(page.locator("#chat")).to_contain_text("浏览器原始事实")
    page.evaluate("""() => {
      window.switchEvents = [];
      CM.on("conversationChanged", payload => window.switchEvents.push(payload));
    }""")

    held = []

    def hold(route):
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("character_id") == [second] and "before_id" not in query and not held:
            held.append(route)
        else:
            route.continue_()

    page.route("**/v1/chat/history-page?*", hold)
    page.evaluate("id => { window.pendingStaleSwitch = CM.switchCharacter(id); }", second)
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(10)
    assert held

    page.evaluate("async id => { await CM.switchCharacter(id); }", first)
    expect(page.locator("#chat")).to_contain_text("浏览器原始事实")
    held.pop().continue_()
    page.evaluate("async () => { await window.pendingStaleSwitch; }")

    assert page.evaluate("CM.state.characterId") == first
    assert page.evaluate("window.switchEvents") == [{"type": "DIRECT", "characterId": first}]
    stream_url = page.evaluate("CM.state.directStream?.url || ''")
    assert first in stream_url and second not in stream_url
