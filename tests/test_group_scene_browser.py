import os
from pathlib import Path

import pytest
from test_space_reconciliation_playwright import space_server as _space_server

pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='explicit browser run')]


@pytest.fixture
def scene_server(tmp_path_factory):
    # Each viewport must exercise first creation, not inherit the prior active scene.
    yield from _space_server.__wrapped__(tmp_path_factory)


@pytest.mark.parametrize('width',[1440,390])
def test_warm_entry_confirm_day_advance_and_resume(page,scene_server,width):
    from playwright.sync_api import expect
    page.set_viewport_size({'width':width,'height':960})
    page.goto(scene_server)
    if width<820:
        page.locator('#sidebarMobileButton').click()
    page.locator('.scene-entry-button').click()
    expect(page.locator('.ensemble-member-card')).to_have_count(4)
    output=os.getenv('SCENE_SCREENSHOTS')
    if output:
        Path(output).mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(Path(output)/f'isolated-preview-{width}.png'),full_page=True)
    page.locator('[data-ensemble-confirm]').click()
    expect(page.locator('.scene-opening')).to_be_visible(timeout=20000)
    expect(page.locator('.scene-toolbar')).to_contain_text('第 1 天傍晚')
    expect(page.locator('[data-scene-say]')).to_have_count(3)
    if output: page.screenshot(path=str(Path(output)/f'isolated-opening-{width}.png'),full_page=True)
    page.locator('[data-scene-advance]').click()
    expect(page.locator('.scene-toolbar')).to_contain_text('第 2 天早晨')
    group_id = page.evaluate('CM.state.conversation.groupId')
    response = page.request.post(f'{scene_server}/v1/groups/{group_id}/scene/advance',
                                 data={'expected_day':2,'request_id':'other-tab'})
    assert response.status == 200
    # This page is now stale. A conflict must refresh its day without advancing again.
    page.locator('[data-scene-advance]').click()
    expect(page.locator('.scene-toolbar')).to_contain_text('第 3 天早晨')
    expect(page.locator('[data-scene-status]')).to_contain_text('同步')
    page.locator('[data-scene-say]').first.click()
    expect(page.locator('.group-mode .chat')).to_contain_text('我推开旅店的门')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if output: page.screenshot(path=str(Path(output)/f'isolated-story-{width}.png'),full_page=True)
    page.reload()
    if width<820: page.locator('#sidebarMobileButton').click()
    page.locator('.scene-entry-button').click()
    expect(page.locator('.scene-toolbar')).to_contain_text('第 3 天早晨')
    expect(page.locator('.chat')).to_contain_text('我推开旅店的门')
