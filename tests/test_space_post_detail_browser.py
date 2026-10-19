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


@pytest.mark.parametrize('width', [390, 1440, 1586])
@pytest.mark.parametrize('kind,count', [('text', 2), ('mixed', 8)])
def test_space_feed_lays_out_real_posts_in_multiple_columns(page, space_server_with_image, width, kind, count):
    base, media_id, _ = space_server_with_image
    post_ids = []
    for index in range(count):
        content = f'Masonry acceptance {index}'
        if index == 1:
            content += '\n' + 'Unequal height content should wrap inside its own card. ' * 8
        payload = {'character_id': 'rin', 'content': content}
        if kind == 'mixed' and index % 3 == 0:
            payload['media_ids'] = [media_id]
        response = page.request.post(base + '/v1/space/posts', data=payload)
        assert response.ok
        post_ids.append(str(response.json()['post']['id']))
    page.set_viewport_size({'width': width, 'height': 844 if width == 390 else 992})
    page.goto(base + '/?view=space')
    feed = page.locator('.space-feed')
    expect(feed).to_be_visible()
    cards = [feed.locator(f'[data-space-post="{post_id}"]') for post_id in post_ids]
    for card in cards:
        expect(card).to_be_visible()
    feed.locator('img').evaluate_all("images => Promise.all(images.map(image => image.decode().catch(() => {})))")
    columns = int(feed.evaluate('element => getComputedStyle(element).columnCount'))
    assert columns == (1 if width == 390 else 3)
    boxes = [card.bounding_box() for card in cards]
    positions = {round(box['x']) for box in boxes}
    if width == 390:
        assert len(positions) == 1
    else:
        assert len(positions) >= 2
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    for card, box in zip(cards, boxes):
        assert box['width'] >= 200
        assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
        assert card.evaluate('element => element.getClientRects().length') == 1
    from test_mobile_views_playwright import _record_visual
    _record_visual(page, f'space-masonry-{kind}-{width}', ('src/character_memory/web/space.css', 'src/character_memory/web/space.js'))
    output = os.getenv('SPACE_MASONRY_SCREENSHOTS')
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(output) / f'{kind}-{width}.png'), full_page=False)


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
    # Desktop detail adds a main-image button for the selected thumbnail; the
    # thumbnail sequence is still the lightbox's two-image set.
    triggers = detail.locator(
        f'[data-space-post="{post_id}"] [data-space-image-open]:not([data-space-gallery-main])'
    )
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


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
@pytest.mark.parametrize("kind", ["image", "text"])
def test_space_detail_visual_evidence(page, space_server_with_image, width, height, kind):
    # Existing isolated browser fixture owns all mutations and model substitutes.
    import hashlib
    import json
    from test_mobile_views_playwright import _visual_run_dir
    ROOT = Path(__file__).resolve().parents[1]
    OUT = _visual_run_dir() / "post-detail"
    OUT.mkdir(parents=True, exist_ok=True)
    base, first_media, second_media = space_server_with_image
    page.set_viewport_size({"width": width, "height": height})
    page.set_default_timeout(10000)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    short = "午后的阅读角。把桌面收拾好，留下了放书和热茶的位置。窗外有风，屋里刚好安静。"
    content = short if kind == "image" else short + "\n\n" + "整理书架时找到一本旧笔记，里面记着上次想尝试的几种做法。我把今天的观察补在最后一页，明天再回来看看。\n" * 8
    body = {"character_id": "rin", "content": content}
    if kind == "image":
        body["media_ids"] = [first_media, second_media]
    created = page.request.post(base + "/v1/space/posts", data=body)
    assert created.ok, created.text()
    post_id = str(created.json()["post"]["id"])
    page.goto(f"{base}/?view=space&space_post={post_id}", wait_until="domcontentloaded")
    detail = page.locator(".space-detail-view")
    expect(detail).to_be_visible()
    expect(detail.locator(".space-content")).to_contain_text("午后的阅读角")
    page.locator("img").evaluate_all("imgs => Promise.all(imgs.map(i => i.decode().catch(() => {})))")
    form = detail.locator(f'[data-space-comment-form="{post_id}"]')
    form.locator(".space-comment-input").fill("这里看起来很适合读书。桌边的小位置也留得刚好。")
    form.locator(".space-comment-submit").click()
    expect(detail.get_by_text("这里看起来很适合读书。桌边的小位置也留得刚好。", exact=True)).to_be_visible()
    stored = page.request.get(f"{base}/v1/space/posts/{post_id}").json()["post"]
    assert any(c["content"].startswith("这里看起来") for c in stored["comments"])
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal overflow"
    scene = f"post-detail-{kind}-{width}x{height}"
    captures = []
    def capture(suffix, full=False):
        path = OUT / f"{scene}-{suffix}.png"
        page.screenshot(path=str(path), full_page=full, animations="disabled")
        captures.append({"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "full_page": full, "at": datetime.now(timezone.utc).isoformat(),
                         "scroll_y": page.evaluate("scrollY")})
    page.evaluate("scrollTo(0,0)")
    capture("initial")
    capture("full", full=True)
    if kind == "image":
        if width > 820:
            # Desktop uses the approved main-image + thumbnail hierarchy.
            expect(detail.locator(".space-media-gallery")).to_have_count(1)
            expect(detail.locator(".space-media-main")).to_have_count(1)
            expect(detail.locator(".space-media-thumb")).to_have_count(2)
            main_image = detail.locator(".space-media-main img")
            first_src = main_image.get_attribute("src")
            second_thumb = detail.locator(".space-media-thumb").nth(1)
            second_thumb.click()
            dialog = page.get_by_role("dialog", name="动态图片预览")
            expect(dialog).to_be_visible()
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()
            expect(second_thumb).to_have_class("space-media-thumb is-selected")
            assert main_image.get_attribute("src") != first_src
        else:
            # Mobile keeps the existing equal grid until its reference is approved.
            expect(detail.locator(".space-media-gallery")).to_have_count(0)
            expect(detail.locator(".space-media-cell")).to_have_count(2)
        trigger = detail.locator('[data-space-image-open]').first
        trigger.click()
        dialog = page.get_by_role("dialog", name="动态图片预览")
        expect(dialog).to_be_visible()
        expect(dialog.locator(".space-lightbox-image")).to_be_visible()
        capture("lightbox")
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
    rendered_media_count = detail.locator('[data-space-image-open]').count()
    assert rendered_media_count == ((3 if width > 820 else 2) if kind == "image" else 0)
    assets = {}
    for path in ["src/character_memory/web/index.html", "src/character_memory/web/space.js",
                 "src/character_memory/web/space.css", "src/character_memory/web/frontend_v2.css",
                 "tests/e2e_space_app.py", "tests/e2e_realtime_app.py"]:
        raw = (ROOT / path).read_bytes()
        assets[path] = hashlib.sha256(raw).hexdigest()
    served = {}
    for url, path in [("/", "src/character_memory/web/index.html"),
                      ("/static/space.js", "src/character_memory/web/space.js"),
                      ("/static/space.css", "src/character_memory/web/space.css")]:
        response = page.request.get(base + url)
        assert response.ok
        payload = response.body().replace(b"\r\n", b"\n")
        committed = subprocess.check_output(["git", "show", f"HEAD:{path}"], cwd=ROOT).replace(b"\r\n", b"\n")
        assert payload == committed, path
        served[path] = hashlib.sha256(payload).hexdigest()
    assert errors == [], errors
    evidence = dict(scene=scene, exact_tested_source_sha=subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        isolated_runtime_identity="Existing e2e fixture starts a new Python process from this checkout; source and served bytes recorded",
        formal_service_runtime_sha=None, browser=page.context.browser.version,
        viewport=page.evaluate("({width:innerWidth,height:innerHeight})"),
        sample=dict(kind=kind, content_length=len(content), requested_media_count=len(body.get("media_ids", [])),
                    rendered_media_count=rendered_media_count,
                    comments_count=len(stored["comments"]), post_id=post_id),
        screenshots=captures, source_sha256=assets, served_sha256=served, errors=errors,
        boundary="Temporary DB/media/personas, deterministic no-provider model; sample repository-owned media. No formal data or paid provider validation.",
        model_visual_verdict="PENDING_LUNA_MAX", human_visual_signoff="PENDING")
    (OUT / f"{scene}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
