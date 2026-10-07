"""Desktop acceptance against the real RSS HTTP API and SQLite repository."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="explicit browser run")]
ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def rss_server(tmp_path_factory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env["RSS_E2E_DB"] = str(tmp_path_factory.mktemp("rss-ui") / "rss.db")
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")])
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "e2e_rss_app:app", "--app-dir", str(ROOT / "tests"), "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urlopen(base + "/v1/rss/sources", timeout=1):
                    break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError("RSS acceptance server exited")
                time.sleep(0.1)
        else:
            raise RuntimeError("RSS acceptance server did not start")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def rss_page(rss_server, page):
    """The page comes from pytest-playwright, not from a browser of our own.

    Running ``sync_playwright()`` here fails the moment this file shares a
    session with the other browser tests: the plugin drives its own sync API
    inside an asyncio loop, and playwright then refuses to start a second one
    ("It looks like you are using Playwright Sync API inside the asyncio
    loop"). It passes when this file runs alone, which is exactly how it
    shipped green and then failed the first time CI ran it. The same trap is
    recorded in tests/test_character_archive_notice_playwright.py.
    """
    page.set_default_timeout(4000)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(rss_server + "/sources")
    yield page


def test_default_today_search_category_and_detail_return(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    expect(page.locator('[data-period="today"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".feed-card")).to_have_count(30)
    expect(page.locator("#feedGrid")).not_to_contain_text("昨天的 AI 新闻")
    page.locator("#feedSearch").fill("react")
    page.locator("#feedSearchForm").evaluate("form => form.requestSubmit()")
    expect(page.locator(".feed-card")).to_have_count(1)
    page.locator('[data-category="product"]').click()
    expect(page.locator("#feedEmpty")).to_be_visible()
    page.locator('[data-category="development"]').click()
    expect(page.locator(".feed-card")).to_have_count(1)
    page.locator(".feed-card").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#articlePanel")).to_be_visible()
    expect(page.locator("#articleBody")).to_contain_text("Feed 提供")
    page.keyboard.press("Escape")
    expect(page.locator("#articlePanel")).not_to_be_visible()
    expect(page.locator("#feedSearch")).to_have_value("react")
    expect(page.locator(".feed-card")).to_be_focused()


def test_pagination_all_articles_and_desktop_layout(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    page.locator("#loadMoreButton").click()
    expect(page.locator(".feed-card")).to_have_count(38)
    expect(page.locator("#loadMoreButton")).not_to_be_visible()
    page.locator('[data-period="all"]').click()
    page.locator("#loadMoreButton").click()
    expect(page.locator("#feedGrid")).to_contain_text("昨天的 AI 新闻")
    expect(page.locator("#feedGrid")).to_contain_text("发布时间未知")
    cards = page.locator(".feed-card")
    first, second = cards.nth(0).bounding_box(), cards.nth(1).bounding_box()
    assert abs(first["y"] - second["y"]) < 2
    assert first["x"] < second["x"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    evidence = os.getenv("RSS_EVIDENCE_DIR")
    if evidence:
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(Path(evidence) / "desktop-feed.png"), full_page=False)
        cards.nth(1).click()
        expect(page.locator("#articlePanel")).to_be_visible()
        page.screenshot(path=str(Path(evidence) / "desktop-detail.png"))


def test_feed_failure_is_retryable(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    expect(page.locator(".feed-card")).to_have_count(30)
    page.route("**/v1/rss/items?**", lambda route: route.fulfill(status=503, json={"detail": "验收暂时失败"}))
    page.locator('[data-period="all"]').click()
    expect(page.locator("#feedStatus")).to_contain_text("验收暂时失败")
    expect(page.locator("#retryFeedButton")).to_be_visible()
    page.unroute("**/v1/rss/items?**")
    page.locator("#retryFeedButton").click()
    expect(page.locator(".feed-card")).to_have_count(30)


def test_subscription_add_toggle_refresh_and_error_are_visible(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    page.locator('[data-view="sources"]').click()
    expect(page.locator("#sourceList")).to_contain_text("最近成功")
    toggle = page.locator("[data-source-toggle]").first
    toggle.uncheck()
    expect(page.locator("[data-source-toggle]").first).not_to_be_checked()
    page.locator("#addSourceButton").click()
    page.locator("#sourceUrl").fill("https://93.184.216.34/fail.xml")
    page.locator("#sourceName").fill("失败源")
    page.locator('#sourceForm button[type="submit"]').click()
    expect(page.locator("#sourceList")).to_contain_text("抓取失败")
    row = page.locator(".source-row").filter(has_text="失败源")
    row.locator("[data-source-refresh]").click()
    expect(row.locator("[data-source-refresh]")).to_be_enabled()
    expect(row).to_contain_text("500")


def test_late_filter_response_cannot_replace_new_search(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    held = []
    page.route("**/v1/rss/items?**", lambda route: held.append(route) if "q=GPT" in route.request.url else route.continue_())
    page.locator("#feedSearch").fill("GPT")
    page.locator("#feedSearchForm").evaluate("form => form.requestSubmit()")
    expect(page.locator("#feedStatus")).to_contain_text("加载文章")
    page.wait_for_function("document.querySelector('#feedSearch').value === 'GPT'")
    # The newer real HTTP result must remain after the old request finishes.
    page.locator("#feedSearch").fill("React")
    page.locator("#feedSearchForm").evaluate("form => form.requestSubmit()")
    expect(page.locator(".feed-card")).to_have_count(1)
    expect(page.locator("#feedGrid")).to_contain_text("React Native")
    assert held
    held[0].fulfill(status=200, json={"items": [{"id": 999, "title": "过期 GPT 结果", "source_name": "旧响应"}], "has_more": False, "query": {}})
    page.unroute("**/v1/rss/items?**")
    expect(page.locator("#feedGrid")).not_to_contain_text("过期 GPT")
    expect(page.locator("#feedGrid")).to_contain_text("React Native")


def test_detail_failure_retry_and_close_during_request(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    expect(page.locator(".feed-card")).to_have_count(30)
    page.route("**/v1/rss/items/1", lambda route: route.fulfill(status=503, json={"detail": "详情暂时失败"}))
    page.locator('[data-item-id="1"]').click()
    expect(page.locator("#articleBody")).to_contain_text("详情暂时失败")
    page.unroute("**/v1/rss/items/1")
    page.locator("[data-article-retry]").click()
    expect(page.locator("#articleBody")).to_contain_text("GPT API")
    page.keyboard.press("Escape")
    held = []
    page.route("**/v1/rss/items/1", lambda route: held.append(route))
    page.locator('[data-item-id="1"]').click()
    expect(page.locator("#articleBody")).to_contain_text("加载文章")
    page.keyboard.press("Escape")
    assert held
    held[0].fulfill(status=200, json={"item": {"title": "关闭后的结果"}})
    expect(page.locator("#articlePanel")).not_to_be_visible()


def test_successful_subscription_fetch_deduplicates_on_refresh(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    page.locator("#addSourceButton").click()
    page.locator("#sourceUrl").fill("https://93.184.216.34/success.xml")
    page.locator('#sourceForm button[type="submit"]').click()
    expect(page.locator("#sourceModal")).not_to_be_visible()
    page.locator('[data-view="sources"]').click()
    row = page.locator(".source-row").filter(has_text="success.xml")
    expect(row).to_contain_text("1 篇")
    row.locator("[data-source-refresh]").click()
    expect(row.locator("[data-source-refresh]")).to_be_enabled()
    expect(row).to_contain_text("1 篇")


def test_today_pagination_restarts_when_effective_day_changes(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    expect(page.locator(".feed-card")).to_have_count(30)
    page.route("**/v1/rss/items?**before_id=**", lambda route: route.fulfill(status=200, json={
        "items": [], "has_more": False, "next_before_id": None,
        "query": {"period": "today", "date": "2099-01-01"},
    }))
    with page.expect_response(lambda response: "/v1/rss/items?" in response.url and "before_id" in response.url):
        page.locator("#loadMoreButton").click()
    expect(page.locator("#feedHint")).not_to_contain_text("2099")
    expect(page.locator("#loadMoreButton")).to_be_visible()
    expect(page.locator(".feed-card")).to_have_count(30)


def test_old_source_snapshot_does_not_overwrite_refresh(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    page.locator('[data-view="sources"]').click()
    expect(page.locator(".source-row").first).to_be_visible()
    held = []

    def hold_first(route):
        if route.request.method == "GET" and not held:
            held.append((route, route.fetch().json()))
        else:
            route.continue_()

    page.route("**/v1/rss/sources", hold_first)
    toggle = page.locator("[data-source-toggle]").first
    toggle.set_checked(not toggle.is_checked())
    expect(page.locator("#sourceStatus")).to_contain_text("加载订阅")
    page.locator("[data-source-refresh]").first.click()
    expect(page.locator(".source-row").first).to_contain_text("新增验收源")
    assert held
    held[0][0].fulfill(status=200, json=held[0][1])
    expect(page.locator(".source-row").first).to_contain_text("新增验收源")


def test_cancel_pending_add_does_not_close_new_dialog(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    held = []
    page.route("**/v1/rss/sources", lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    page.locator("#addSourceButton").click()
    page.locator("#sourceUrl").fill("https://93.184.216.34/pending.xml")
    page.locator('#sourceForm button[type="submit"]').click()
    expect(page.locator('#sourceForm button[type="submit"]')).to_be_disabled()
    page.locator("#cancelSourceButton").click()
    page.locator("#addSourceButton").click()
    page.locator("#sourceUrl").fill("https://93.184.216.34/new-draft.xml")
    expect(page.locator('#sourceForm button[type="submit"]')).to_be_enabled()
    assert held
    held[0].fulfill(status=200, json={"source": {"id": 999}, "refresh": {"ok": True}})
    expect(page.locator("#sourceModal")).to_be_visible()
    expect(page.locator("#sourceUrl")).to_have_value("https://93.184.216.34/new-draft.xml")


def test_article_displays_paragraphs_headings_lists_and_inline_images(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    expect(page.locator(".feed-card")).to_have_count(30)
    page.locator('[data-item-id="1"]').click()
    expect(page.locator("#articleBody h2")).to_have_text("正文小标题")
    expect(page.locator(".article-rich p")).to_have_count(3)
    expect(page.locator(".article-rich li")).to_have_count(2)
    expect(page.locator(".article-rich img")).to_have_count(2)
    expect(page.locator(".article-rich img").first).to_be_visible()
    assert page.locator(".article-rich img").first.evaluate("img => img.complete && img.naturalWidth > 0")
    # The source's first image belongs in the body once, not again as a detail hero.
    expect(page.locator(".article-image")).to_have_count(0)
    evidence = os.getenv("RSS_EVIDENCE_DIR")
    if evidence:
        page.screenshot(path=str(Path(evidence) / "formatted-content.png"))


def test_image_proxy_preserves_recorded_unicode_url(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    raw = 'https://EXAMPLE.COM/中文 photo.png'
    requested = []
    def article(route):
        response = route.fetch()
        data = response.json()
        data['item']['content_html'] = f'<p>中文图片</p><img src="{raw}">'
        data['item']['image_url'] = raw
        route.fulfill(response=response, json=data)
    def image(route):
        from urllib.parse import parse_qs, urlparse
        requested.append(parse_qs(urlparse(route.request.url).query)['url'][0])
        route.fulfill(status=200, content_type='image/png', body=__import__('base64').b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jCwkAAAAASUVORK5CYII='))
    page.route('**/v1/rss/items/1', article)
    page.route('**/v1/rss/items/1/image?*', image)
    page.locator('[data-item-id="1"]').click()
    expect(page.locator('.article-rich img')).to_be_visible()
    page.wait_for_function('document.querySelector(".article-rich img").naturalWidth > 0')
    assert requested == [raw]


def test_subscription_navigation_scopes_search_and_pagination(rss_page, rss_server):
    from playwright.sync_api import expect
    page = rss_page
    response = page.request.post(rss_server + '/v1/rss/sources', data={
        'feed_url': 'https://93.184.216.34/navigation.xml', 'name': '筛选验收源',
    })
    assert response.ok
    source = response.json()['source']
    first_source = next(s for s in page.request.get(rss_server + '/v1/rss/sources').json()['sources'] if s['id'] == 1)
    page.reload()
    selected = page.locator(f'[data-feed-source="{source["id"]}"]')
    selected.click()
    expect(selected).to_be_focused()
    expect(selected).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('[data-period="all"]')).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('.feed-card')).to_have_count(1)
    expect(page.locator('#feedGrid')).to_contain_text('新增产品文章')
    expect(page.locator('#viewTitle')).to_have_text(source['name'])
    page.locator('[data-feed-source="1"]').click()
    expect(page.locator('.feed-card')).to_have_count(30)
    page.locator('#loadMoreButton').click()
    expect(page.locator('.feed-card')).to_have_count(first_source['item_count'])
    assert set(page.locator('.feed-source').all_text_contents()) == {first_source['name']}
    page.locator('#feedSearch').fill('React')
    page.locator('#feedSearchForm').evaluate('form => form.requestSubmit()')
    page.locator('[data-category="development"]').click()
    expect(page.locator('.feed-card')).to_have_count(1)
    page.locator('.feed-card').click()
    page.locator('#closeArticleButton').click()
    expect(page.locator('[data-feed-source="1"]')).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('#feedSearch')).to_have_value('React')
    page.locator('[data-feed-source=""]').click()
    expect(page.locator('#viewTitle')).to_have_text('外部信息')
    expect(page.locator('.feed-card')).to_have_count(1)
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(page.locator('[data-feed-source="1"]')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_original_link_is_below_article_content(rss_page):
    from playwright.sync_api import expect
    page = rss_page
    page.locator('[data-item-id="1"]').click()
    footer = page.locator('#articleBody .article-original')
    footer.scroll_into_view_if_needed()
    expect(footer.get_by_role('link', name='查看原文')).to_have_attribute('href', 'https://example.com/article/0')
    expect(footer.get_by_role('link')).to_have_attribute('target', '_blank')
    assert footer.evaluate('el => el.previousElementSibling.classList.contains("article-rich")')
