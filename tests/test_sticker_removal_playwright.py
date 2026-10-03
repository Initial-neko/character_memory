"""PC removal on an isolated real Core/browser fixture, without model calls."""
import os
import pytest
from urllib.parse import urljoin
from test_chat_ui_playwright import chat, chat_server

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="dedicated browser validation")]


def test_pc_manage_cancel_single_pack_and_history_asset(chat):
    page = chat
    page.locator(".sticker-trigger").click()
    page.locator("[data-sticker-manage]").click()
    button = page.locator("[data-sticker-id]").first
    sticker_id = button.get_attribute("data-sticker-id")
    asset_url = urljoin(page.url, button.locator("img").get_attribute("src"))
    original = page.request.get(asset_url).body()
    before = page.locator("[data-sticker-id]").count()
    page.once("dialog", lambda dialog: dialog.dismiss())
    button.click()
    assert page.locator("[data-sticker-id]").count() == before
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.request.method == "DELETE") as deletion:
        button.click()
    assert deletion.value.status == 200
    page.wait_for_function("id => !document.querySelector(`[data-sticker-id=\"${id}\"]`)", arg=sticker_id)
    assert page.request.get(asset_url).body() == original
    pack_ids = page.locator("[data-sticker-id]").evaluate_all("els => els.map(el => el.dataset.stickerId)")
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.request.method == "DELETE") as deletion:
        page.locator("[data-sticker-remove-pack]").click()
    assert deletion.value.status == 200
    available = {item["id"] for item in page.request.get(urljoin(page.url, "/v1/stickers")).json()["stickers"]}
    assert not set(pack_ids) & available
    page.locator("[data-sticker-manage]").click()
    page.reload()
    available = {item["id"] for item in page.request.get(urljoin(page.url, "/v1/stickers")).json()["stickers"]}
    assert sticker_id not in available
