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


@pytest.mark.parametrize('width', [1440, 390])
def test_text_detail_is_one_reading_column_with_comments_below(page, space_server, width):
    page.set_viewport_size({'width': width, 'height': 960})
    post_id = _create_post(page, space_server, '第二锅饭还在蒸。空档里翻了几个炖菜的做法。\n下周三想试试，盐和酱油还得自己慢慢调。')
    page.goto(f'{space_server}/?view=space&space_post={post_id}')
    detail = page.locator('.space-post-detail-text-only')
    expect(detail).to_be_visible()
    head = detail.locator('.space-post-head').bounding_box()
    body = detail.locator('.space-content').bounding_box()
    comments = detail.locator('.space-comments').bounding_box()
    assert head['y'] + head['height'] <= body['y'] + 1
    assert body['y'] + body['height'] <= comments['y'] + 20
    assert detail.bounding_box()['width'] <= 822
    assert detail.locator('.space-content').evaluate('el => parseFloat(getComputedStyle(el).fontSize)') == (15 if width < 560 else 16)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    expect(page.locator('.space-page-heading')).to_be_hidden()


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
            storage = MediaStorage(child_env["CHARACTER_MEMORY_E2E_MEDIA"])
            assets = []
            for name in ("space-browser-test-a.png", "space-browser-test-b.png"):
                asset = storage.save_bytes(
                    character_id="rin",
                    original_name=name,
                    payload=image_path.read_bytes(),
                    created_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
                    source="GENERATED_TEST",
                )
                store.add_media_asset(asset)
                assets.append(asset)
        finally:
            store.close()
        yield base, assets[0].id, assets[1].id
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
    expect(local_nav.get_by_role("button", name="全部", exact=True)).to_be_visible()
    expect(local_nav.get_by_role("button", name="当前人物")).to_be_visible()
    expect(local_nav.get_by_role("button", name="世界来客")).to_be_visible()

    current_button = local_nav.get_by_role("button", name="当前人物")
    with page.expect_response(lambda response: "/v1/space/posts?" in response.url and "character_id=rin" in response.url):
        current_button.click()
    expect(page.locator(f'.space-feed [data-space-post="{post_ids[0]}"]')).to_be_visible()
    page.locator(f'.space-feed [data-space-post="{post_ids[0]}"] .space-content').click()
    expect(page.locator(".space-detail-view")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".space-detail-view")).to_be_hidden()
    expect(current_button).to_have_attribute("aria-current", "page")

    all_button = local_nav.get_by_role("button", name="全部", exact=True)
    with page.expect_response(lambda response: "/v1/space/feed?" in response.url and "character_id=" not in response.url):
        all_button.click()
    expect(page.locator(f'.space-feed [data-space-post="{post_ids[1]}"]')).to_be_visible()

    local_nav.get_by_role("button", name="世界来客").click()
    expect(page.locator(".cm-world-page")).to_be_visible()
    expect(page.locator(".space-shell")).to_be_hidden()


def test_leaving_scrolled_detail_does_not_restore_feed_scroll_into_world(page, space_server):
    page.set_viewport_size({'width': 1440, 'height': 720})
    candidates = [{'id': i, 'status': 'NEW', 'draft': {'name': f'Guest {i}', 'identity': '修理旧机器的人'},
        'portrait_url': '/static/call-room.png', 'encounter_hook': '候选介绍' * 30,
        'opening_message': '你好', 'can_accept': True} for i in range(3)]
    page.route('**/v1/encounters?*', lambda route: route.fulfill(json={
        'encounters': candidates, 'active_character_count': 1, 'active_character_limit': 20}))
    ids = [_create_post(page, space_server, f'滚动来源 {i}\n' + '饭蒸好了，稍后一起看看。\n' * 16) for i in range(12)]
    page.goto(space_server + '/?view=space')
    card = page.locator(f'.space-feed [data-space-post="{ids[0]}"]')
    expect(card).to_be_visible()
    card.get_by_role('button', name='打开 Rin 的动态详情').click()
    expect(page.locator('.space-detail-view')).to_be_visible()
    page.locator('.cm-global-nav').get_by_role('link', name='World', exact=True).click()
    expect(page.locator('.cm-world-page')).to_be_visible()
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert page.evaluate('scrollY') < 5


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
    card.locator(".space-content").click()

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
    base, media_id, _second_media_id = space_server_with_image
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


def test_space_lightbox_moves_between_real_post_images(page, space_server_with_image):
    base, first_media_id, second_media_id = space_server_with_image
    created = page.request.post(
        f"{base}/v1/space/posts",
        data={
            "character_id": "rin",
            "content": "两张测试图片的灯箱导航",
            "media_ids": [first_media_id, second_media_id],
        },
    )
    assert created.ok
    post_id = str(created.json()["post"]["id"])
    page.goto(f"{base}/?space_post={post_id}", wait_until="domcontentloaded")

    detail = page.locator(".space-detail-view")
    expect(detail).to_be_visible()
    triggers = detail.locator(f'[data-space-post="{post_id}"] [data-space-image-open]')
    expect(triggers).to_have_count(2)
    triggers.nth(0).click()
    dialog = page.get_by_role("dialog", name="动态图片预览")
    expect(dialog.locator("[data-space-lightbox-count]")).to_have_text("1 / 2")
    first_url = triggers.nth(0).get_attribute("data-image-url")
    second_url = triggers.nth(1).get_attribute("data-image-url")
    expect(dialog.locator(".space-lightbox-image")).to_have_attribute("src", first_url)

    dialog.get_by_role("button", name="下一张").click()
    expect(dialog.locator("[data-space-lightbox-count]")).to_have_text("2 / 2")
    expect(dialog.locator(".space-lightbox-image")).to_have_attribute("src", second_url)
    dialog.get_by_role("button", name="上一张").click()
    expect(dialog.locator("[data-space-lightbox-count]")).to_have_text("1 / 2")
    expect(dialog.locator(".space-lightbox-image")).to_have_attribute("src", first_url)

    dialog.get_by_role("button", name="关闭", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(triggers.nth(0)).to_be_focused()


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


def test_space_comment_reply_and_thread_controls_work_in_browser(page, space_server):
    created = page.request.post(
        f"{space_server}/v1/space/posts",
        data={"character_id": "rin", "content": "评论线程按钮真实行为测试"},
    )
    assert created.ok
    post_id = str(created.json()["post"]["id"])
    roots = []
    for index in range(4):
        response = page.request.post(
            f"{space_server}/v1/space/posts/{post_id}/comments",
            data={"content": f"测试主评论 {index + 1}"},
        )
        assert response.ok
        roots.append(response.json()["comment"])
    replies = []
    for index in range(4):
        response = page.request.post(
            f"{space_server}/v1/space/posts/{post_id}/comments",
            data={"content": f"根评论的回复 {index + 1}", "reply_to_comment_id": roots[0]["id"]},
        )
        assert response.ok
        replies.append(response.json()["comment"])

    page.goto(space_server, wait_until="domcontentloaded")
    page.locator(".cm-global-nav").get_by_role("button", name="Space", exact=True).click()
    post = page.locator(f'.space-feed [data-space-post="{post_id}"]')
    post.locator("[data-space-comments-panel-toggle]").click()

    # The collapsed panel opens, and root/thread disclosure controls change the
    # rendered comments rather than only changing their labels.
    root_toggle = post.get_by_role("button", name="查看全部 4 条主评论")
    expect(root_toggle).to_be_visible()
    assert post.locator(".space-comment-thread").count() == 3
    root_toggle.click()
    assert post.locator(".space-comment-thread").count() == 4
    post.locator("[data-space-comments-toggle]").click()
    assert post.locator(".space-comment-thread").count() == 3

    target_thread = post.locator(f'[data-space-thread-root="{roots[0]["id"]}"]')
    expect(target_thread.get_by_text("展开 2 条回复", exact=True)).to_be_visible()
    assert target_thread.locator(".space-comment-reply").count() == 2
    target_thread.get_by_role("button", name="展开 2 条回复").click()
    assert target_thread.locator(".space-comment-reply").count() == 4
    target_thread.get_by_role("button", name="收起回复").click()
    assert target_thread.locator(".space-comment-reply").count() == 2

    target_thread.locator(f'[data-space-reply-comment="{roots[0]["id"]}"]').click()
    form = post.locator(f'[data-space-comment-form="{post_id}"]')
    expect(form.locator("[data-space-reply-cancel]")).to_be_visible()
    expect(form.locator(".space-comment-input")).to_have_attribute("placeholder", "回复 我…")
    form.get_by_role("button", name="取消", exact=True).click()
    expect(form.locator("[data-space-reply-cancel]")).to_have_count(0)
    expect(form.locator(".space-comment-input")).to_have_attribute("placeholder", "评论这条动态…")

    # Reply submission preserves the selected parent ID in the durable record.
    target_thread.locator(f'[data-space-reply-comment="{roots[0]["id"]}"]').click()
    form.locator(".space-comment-input").fill("浏览器发出的真实楼中楼回复")
    form.locator(".space-comment-submit").click()
    expect(post.get_by_text("浏览器发出的真实楼中楼回复", exact=True)).to_be_visible()
    stored = page.request.get(f"{space_server}/v1/space/posts/{post_id}").json()["post"]
    reply = next(item for item in stored["comments"] if item["content"] == "浏览器发出的真实楼中楼回复")
    assert str(reply["reply_to_comment_id"]) == str(roots[0]["id"])
    assert {str(item["id"]) for item in replies}.issubset(
        {str(item["id"]) for item in stored["comments"]}
    )
