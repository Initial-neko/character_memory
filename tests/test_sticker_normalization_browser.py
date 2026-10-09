import os
from urllib.parse import urljoin
import pytest
from test_chat_ui_playwright import chat, chat_server
from test_sticker_sheet import sheet,png
from character_memory.sticker_sheet import _decode

pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='isolated browser validation')]


def test_png_normalization_option_is_explicit_and_reaches_real_import(chat):
    from playwright.sync_api import expect
    page=chat
    width,height,rgba=_decode(sheet())
    for pos in range(width*height):
        if rgba[pos*4+3]==0:rgba[pos*4:pos*4+4]=bytes((0,0,0,255))
    data=png(width,height,rgba)
    page.locator('input.sticker-import-input').set_input_files({'name':'solid.png','mimeType':'image/png','buffer':data})
    normalize=page.locator('[data-sticker-normalize-background]')
    expect(normalize).not_to_be_checked()
    expect(page.locator('.sticker-import-card')).to_contain_text('同色贴边图案可能被清除')
    normalize.check();page.locator('[data-sticker-auto-tag]').uncheck()
    with page.expect_response(lambda response:'/v1/stickers/import?' in response.url) as imported:
        page.locator('[data-sticker-import-confirm]').click()
    assert 'normalize_background=true' in imported.value.url
    assert 'auto_tag=false' in imported.value.url
    assert imported.value.status==200 and imported.value.json()['imported']==9
    expect(page.locator('.sticker-import-notice')).to_contain_text('9')
