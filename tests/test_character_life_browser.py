import os
from pathlib import Path
import socket
import threading
import time
from urllib.request import urlopen
import pytest

pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='explicit browser run')]

@pytest.fixture(scope='module')
def life_server(tmp_path_factory):
    import uvicorn
    from e2e_character_life_app import build
    app,store=build(tmp_path_factory.mktemp('life-ui')/'life.db')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,name='life-browser-isolated-server')
    thread.start();base=f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            if server.started:break
            if not thread.is_alive():raise RuntimeError('isolated life server exited')
            time.sleep(.05)
        else:raise RuntimeError('isolated life server did not start')
        yield base
    finally:
        server.should_exit=True;thread.join(timeout=10)
        assert not thread.is_alive()
        store.close()
        with socket.socket() as sock:assert sock.connect_ex(('127.0.0.1',port))!=0

@pytest.fixture
def life_page(life_server,page):
    page.set_viewport_size({'width':1600,'height':1000})
    page.goto(life_server+'/life')
    from playwright.sync_api import expect
    expect(page.locator('#timeline .event-card').first).to_be_visible()
    return page


def test_filters_details_state_memory_actual_no_action(life_page):
    from playwright.sync_api import expect
    p=life_page
    p.locator('[data-channel="SPACE"]').click()
    expect(p.locator('.event-card')).to_have_count(1)
    expect(p.locator('#details')).to_contain_text('对冷暖配色感兴趣')
    expect(p.locator('#details')).to_contain_text('讨论过冷暖配色')
    p.locator('[data-channel="WORLD"]').click()
    expect(p.locator('.event-card')).to_have_count(2)
    p.get_by_role('heading',name='阅读了 RSS Feed 文本：配色笔记').locator('..').locator('..').click()
    expect(p.locator('#details')).to_contain_text('RSS_FEED_TEXT')
    expect(p.get_by_role('link',name='查看原文')).to_have_attribute('href','https://example.com/article')
    p.locator('[data-channel="ALL"]').click()
    p.locator('[data-view="intents"]').click()
    expect(p.locator('.event-card')).to_have_count(1)
    expect(p.locator('#details')).to_contain_text('未来计划不是已经发生的经历')
    expect(p.get_by_role('button',name='围绕这件事聊天')).to_be_disabled()


def test_paging_search_character_switch_and_mobile(life_page):
    from playwright.sync_api import expect
    p=life_page
    expect(p.locator('.event-card')).to_have_count(30)
    p.locator('#more').click()
    expect(p.locator('.event-card')).to_have_count(39)
    p.locator('#search').fill('真实分页测试 12');p.locator('#searchForm').evaluate('f=>f.requestSubmit()')
    expect(p.locator('.event-card')).to_have_count(1)
    p.locator('#search').fill('');p.locator('#searchForm').evaluate('f=>f.requestSubmit()')
    p.locator('#character').select_option('b')
    expect(p.locator('.event-card')).to_have_count(1)
    expect(p.locator('#timeline')).not_to_contain_text('配色')
    p.set_viewport_size({'width':390,'height':844})
    assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')


def test_visual_and_failure_retry(life_page):
    from playwright.sync_api import expect
    p=life_page
    p.locator('[data-channel="SPACE"]').click()
    expect(p.locator('.event-card')).to_have_count(1)
    folder=os.getenv('LIFE_EVIDENCE_DIR')
    if folder:
        Path(folder).mkdir(parents=True,exist_ok=True)
        p.screenshot(path=str(Path(folder)/'desktop-life.png'),full_page=True)
        p.set_viewport_size({'width':390,'height':844})
        p.screenshot(path=str(Path(folder)/'mobile-life.png'),full_page=True)
    p.route('**/v1/characters/a/life?**',lambda r:r.fulfill(status=503,body='unavailable'))
    p.locator('#today').click()
    expect(p.locator('#retry')).to_be_visible()
    expect(p.locator('.event-card')).to_have_count(0)
    p.unroute('**/v1/characters/a/life?**')
    p.locator('#retry').click()
    expect(p.locator('.event-card')).to_have_count(1)


def test_shared_shell_profile_rail_and_mobile_layout(life_page):
    from playwright.sync_api import expect
    p=life_page
    expect(p.locator(".cm-global-header")).to_be_visible()
    expect(p.locator("#lifeCharacters")).to_contain_text("林绘")
    expect(p.locator("#lifeCharacters")).to_contain_text("另一位角色")
    rail=p.locator(".life-nav").bounding_box()
    assert rail["y"] >= 60
    p.locator("[data-life-character='b']").click()
    expect(p.locator("#character")).to_have_value("b")
    expect(p.locator("#timeline")).to_contain_text("另一个人物独立经历")
    p.set_viewport_size({"width":390,"height":844})
    assert p.evaluate("document.documentElement.scrollWidth<=innerWidth")
    mobile=p.evaluate("""() => ({
      globalBottom: document.querySelector('.cm-global-header').getBoundingClientRect().bottom,
      lifeNavigationTop: document.querySelector('.life-page-nav').getBoundingClientRect().top
    })""")
    assert mobile["lifeNavigationTop"] >= mobile["globalBottom"]


def test_desktop_identity_and_timeline_fit_their_layout(life_page):
    p=life_page
    layout=p.evaluate("""() => {
      const rect = selector => document.querySelector(selector).getBoundingClientRect();
      const globalHeader = rect('.cm-global-header');
      const identity = rect('.life-header');
      const records = rect('.records');
      const details = rect('.details');
      const active = document.querySelector('.life-page-nav [aria-current="page"]');
      const activeColor = getComputedStyle(active).color;
      const accentProbe = document.createElement('span');
      accentProbe.style.color = getComputedStyle(document.body).getPropertyValue('--ui-accent').trim();
      document.body.append(accentProbe);
      const accentColor = getComputedStyle(accentProbe).color;
      accentProbe.remove();

      const card = document.querySelector('#timeline .event-card');
      const paragraph = card.querySelector('p');
      paragraph.textContent = 'LongRecordWithoutSpaces'.repeat(80);
      const cardRect = card.getBoundingClientRect();
      const paragraphRect = paragraph.getBoundingClientRect();
      return {
        identityTop: identity.top,
        globalHeaderBottom: globalHeader.bottom,
        activeColor,
        accentColor,
        recordsRight: records.right,
        detailsLeft: details.left,
        paragraphClientWidth: paragraph.clientWidth,
        paragraphScrollWidth: paragraph.scrollWidth,
        paragraphRight: paragraphRect.right,
        cardRight: cardRect.right,
        documentScrollWidth: document.documentElement.scrollWidth,
        viewportWidth: innerWidth
      };
    }""")
    assert layout["identityTop"] >= layout["globalHeaderBottom"] + 8
    assert layout["activeColor"] == layout["accentColor"]
    assert layout["detailsLeft"] >= layout["recordsRight"]
    assert layout["paragraphClientWidth"] > 0
    assert layout["paragraphScrollWidth"] <= layout["paragraphClientWidth"]
    assert layout["paragraphRight"] <= layout["cardRight"]
    assert layout["documentScrollWidth"] <= layout["viewportWidth"]


def test_archived_and_unknown_characters_never_show_another_character(life_server, page):
    from playwright.sync_api import expect
    page.set_viewport_size({"width":1600,"height":1000})

    page.goto(life_server + '/life?character=c')
    expect(page.locator('#heading')).to_contain_text('归档人物')
    expect(page.locator('#timeline')).to_contain_text('归档人物的独立经历')
    expect(page.locator('#timeline')).not_to_contain_text('真实分页测试')
    expect(page.locator('#character')).to_have_value('c')

    page.goto(life_server + '/life?character=ghost')
    expect(page.locator('#status')).to_contain_text('不存在')
    expect(page.locator('#timeline')).not_to_contain_text('真实分页测试')
    expect(page.locator('#timeline .event-card')).to_have_count(0)
