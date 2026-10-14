"""Browser checks for the Space detail route and canonical post/comment DOM."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess

import pytest

from character_memory.media import MediaStorage
from character_memory.storage.sqlite import SQLiteStore
import test_space_reconciliation_playwright as reconciliation_tests
from test_space_reconciliation_playwright import space_server


if os.getenv("RUN_PLAYWRIGHT") == "1":
    from playwright.sync_api import expect
else:
    expect = None

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="explicit browser run"),
]


def _create_post(page, base: str, content: str) -> str:
    response = page.request.post(
        f"{base}/v1/space/posts",
        data={"character_id": "rin", "content": content},
    )
    assert response.ok
    return str(response.json()["post"]["id"])


@pytest.fixture
def space_server_with_image(tmp_path_factory, monkeypatch):
    """Start the isolated browser server and seed one repository-owned image."""

    child_env: dict[str, str] = {}
    real_popen = subprocess.Popen

    def capture_space_server_env(*args, **kwargs):
        env = kwargs.get("env") or {}
        if "CHARACTER_MEMORY_E2E_DB" in env:
            child_env.update(env)
        return real_popen(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(reconciliation_tests.subprocess, "Popen", capture_space_server_env)
        fixture = space_server.__wrapped__(tmp_path_factory)
        base = next(fixture)

    try:
        store = SQLiteStore(child_env["CHARACTER_MEMORY_E2E_DB"])
        try:
            image_path = Path(__file__).resolve().parents[1] / "src" / "character_memory" / "web" / "call-room.png"
            asset = MediaStorage(child_env["CHARACTER_MEMORY_E2E_MEDIA"]).save_bytes(
                character_id="rin",
                original_name="space-browser-test.png",
                payload=image_path.read_bytes(),
                created_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
                source="GENERATED_TEST",
            )
            store.add_media_asset(asset)
        finally:
            store.close()
        yield base, asset.id
    finally:
        fixture.close()


def test_space_feed_lays_out_real_posts_in_multiple_columns(page, space_server):
    post_ids = [_create_post(page, space_server, f"瀑布流真实动态 {index}") for index in range(4)]
    page.set_viewport_size({"width": 1586, "height": 992})
    page.goto(space_server, wait_until="domcontentloaded")
    page.locator(".cm-global-nav").get_by_role("button", name="Space", exact=True).click()

    feed = page.locator(".space-feed")
    expect(feed).to_be_visible()
    expect(page.get_by_role("heading", name="Space", exact=True)).to_be_visible()
    for post_id in post_ids:
        expect(feed.locator(f'[data-space-post="{post_id}"]')).to_be_visible()
        assert feed.locator(f'[data-space-post="{post_id}"]').bounding_box()['width'] >= 200
    columns = int(feed.evaluate("element => getComputedStyle(element).columnCount"))
    assert columns == 3


def test_space_local_navigation_uses_existing_feed_and_world_routes(page, space_server):
    post_ids = [_create_post(page, space_server, f"本地导航测试动态 {index}") for index in range(2)]
    page.goto(space_server, wait_until="domcontentloaded")
    page.locator(".cm-global-nav").get_by_role("button", name="Space", exact=True).click()

    local_nav = page.get_by_role("navigation", name="Space 页面导航")
    expect(local_nav.get_by_role("button", name="全部动态")).to_be_visible()
    expect(local_nav.get_by_role("button", name="当前人物")).to_be_visible()
    expect(local_nav.get_by_role("button", name="世界来客")).to_be_visible()

    current_button = local_nav.get_by_role("button", name="当前人物")
    with page.expect_response(lambda response: "/v1/space/posts?" in response.url and "character_id=rin" in response.url):
        current_button.click()
    expect(page.locator(f'.space-feed [data-space-post="{post_ids[0]}"]')).to_be_visible()
    page.locator(f'.space-feed [data-space-post="{post_ids[0]}"]').click(position={"x": 20, "y": 20})
    expect(page.locator(".space-detail-view")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".space-detail-view")).to_be_hidden()
    expect(current_button).to_have_attribute("aria-current", "page")

    all_button = local_nav.get_by_role("button", name="全部动态")
    with page.expect_response(lambda response: "/v1/space/posts?" in response.url and "character_id=" not in response.url):
        all_button.click()
    expect(page.locator(f'.space-feed [data-space-post="{post_ids[1]}"]')).to_be_visible()

    local_nav.get_by_role("button", name="世界来客").click()
    expect(page.locator(".cm-world-page")).to_be_visible()
    expect(page.locator(".space-shell")).to_be_hidden()


def test_space_card_detail_returns_to_same_feed_position(page, space_server):
    post_id = _create_post(page, space_server, "可从卡片打开并返回的详情测试")
    page.goto(space_server, wait_until="domcontentloaded")
    page.locator(".cm-global-nav").get_by_role("button", name="Space", exact=True).click()

    card = page.locator(f'[data-space-post="{post_id}"]')
    expect(card).to_be_visible()
    assert card.evaluate("element => element.tabIndex") == -1
    assert card.get_attribute("role") is None
    card.scroll_into_view_if_needed()
    before = page.evaluate("window.scrollY")
    card.click(position={"x": 20, "y": 20})

    detail = page.locator(".space-detail-view")
    expect(detail).to_be_visible()
    expect(detail.locator(f'[data-space-post="{post_id}"]')).to_be_visible()
    assert page.evaluate("new URL(location.href).searchParams.get('space_post')") == post_id
    expect(detail.locator(f'[data-space-comment-form="{post_id}"]')).to_have_count(1)

    detail.get_by_role("button", name="返回 Space").click()
    expect(detail).to_be_hidden()
    expect(card).to_be_visible()
    assert abs(page.evaluate("window.scrollY") - before) <= 4
    assert page.evaluate("new URL(location.href).searchParams.has('space_post')") is False
    detail_trigger = card.get_by_role("button", name="打开 Rin 的动态详情")
    expect(detail_trigger).to_be_focused()
    page.keyboard.press("Enter")
    expect(detail).to_be_visible()
    page.keyboard.press("Escape")
    expect(detail).to_be_hidden()
    assert page.evaluate("new URL(location.href).searchParams.has('space_post')") is False


def test_space_lightbox_traps_focus_and_restores_image_trigger(page, space_server_with_image):
    base, media_id = space_server_with_image
    created = page.request.post(
        f"{base}/v1/space/posts",
        data={"character_id": "rin", "content": "灯箱真实图片焦点测试", "media_ids": [media_id]},
    )
    assert created.ok
    post_id = str(created.json()["post"]["id"])
    page.goto(base, wait_until="domcontentloaded")
    page.locator(".cm-global-nav").get_by_role("button", name="Space", exact=True).click()

    image_trigger = page.locator(f'.space-feed [data-space-post="{post_id}"] [data-space-image-open]')
    expect(image_trigger).to_be_visible()
    image_trigger.click()
    dialog = page.get_by_role("dialog", name="动态图片预览")
    expect(dialog).to_be_visible()
    close_button = dialog.get_by_role("button", name="关闭", exact=True)
    expect(close_button).to_be_focused()
    page.evaluate("selector => document.querySelector(selector).focus()", f'.space-feed [data-space-post="{post_id}"] [data-space-image-open]')
    expect(close_button).to_be_focused()

    # The only visible controls are the close button and backdrop; Tab wraps
    # inside the modal in both directions instead of reaching the application.
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="关闭预览")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(close_button).to_be_focused()

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert page.locator(".chat-shell").evaluate("element => element.inert") is False
    expect(image_trigger).to_be_focused()


def test_space_detail_deep_link_survives_reload_and_posts_comment(page, space_server):
    post_id = _create_post(page, space_server, "刷新后仍能打开并评论的详情测试")
    page.goto(f"{space_server}/?space_post={post_id}", wait_until="domcontentloaded")

    detail = page.locator(".space-detail-view")
    expect(detail).to_be_visible()
    form = detail.locator(f'[data-space-comment-form="{post_id}"]')
    expect(form).to_have_count(1)
    form.locator(".space-comment-input").fill("详情页真实写入的评论")
    form.locator(".space-comment-submit").click()
    expect(detail.get_by_text("详情页真实写入的评论", exact=True)).to_be_visible()
    stored = page.request.get(f"{space_server}/v1/space/posts/{post_id}").json()["post"]
    assert any(item["content"] == "详情页真实写入的评论" for item in stored["comments"])

    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".space-detail-view")).to_be_visible()
    expect(page.locator(f'[data-space-post="{post_id}"] .space-comment-input')).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".space-detail-view")).to_be_hidden()
    assert page.evaluate("new URL(location.href).searchParams.has('space_post')") is False


def test_space_comment_enter_waits_for_ime_composition(page, space_server):
    post_id = _create_post(page, space_server, "IME 安全浏览器测试")
    page.goto(f"{space_server}/?space_post={post_id}", wait_until="domcontentloaded")

    detail = page.locator(".space-detail-view")
    expect(detail).to_be_visible()
    textarea = detail.locator(f'[data-space-comment-form="{post_id}"] .space-comment-input')
    draft = "输入法候选确认后再发送"
    textarea.fill(draft)

    composing_result = textarea.evaluate("""input => {
      const event = new KeyboardEvent("keydown", {
        key:"Enter", bubbles:true, cancelable:true, isComposing:true,
      });
      input.dispatchEvent(event);
      return {prevented:event.defaultPrevented, value:input.value};
    }""")
    legacy_composing_result = textarea.evaluate("""input => {
      const event = new KeyboardEvent("keydown", {
        key:"Enter", keyCode:229, charCode:13, bubbles:true, cancelable:true,
      });
      input.dispatchEvent(event);
      return {prevented:event.defaultPrevented, keyCode:event.keyCode, value:input.value};
    }""")
    assert composing_result == {"prevented": False, "value": draft}
    assert legacy_composing_result == {"prevented": False, "keyCode": 229, "value": draft}
    assert page.request.get(f"{space_server}/v1/space/posts/{post_id}").json()["post"]["comments"] == []

    textarea.focus()
    page.keyboard.press("Enter")
    expect(detail.get_by_text(draft, exact=True)).to_be_visible()
    stored = page.request.get(f"{space_server}/v1/space/posts/{post_id}").json()["post"]
    assert any(item["content"] == draft for item in stored["comments"])
