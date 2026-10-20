"""Approved desktop composition against the isolated, actually served app."""
import json
import os
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_space_mixed_feed_browser import mixed_server

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="explicit browser run")]


def settled_drawer(page):
    page.wait_for_function("""() => {
      const box = document.querySelector('.drawer.open')?.getBoundingClientRect();
      return box && box.left >= 0 && Math.abs(box.right - innerWidth) < 1;
    }""")
    return page.locator('.drawer.open').bounding_box()


def assert_inside_viewport(box, width, height):
    assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
    assert box['y'] >= 0 and box['y'] + box['height'] <= height + 1


@pytest.mark.parametrize("width,height", [(1440, 900), (1920, 1080)])
def test_desktop_space_and_builder_actions_remain_reachable(page, mixed_server, width, height):
    from playwright.sync_api import expect

    base, _ = mixed_server
    page.set_viewport_size({"width": width, "height": height})
    for index in range(7):
        result = page.request.post(base + "/v1/space/posts", data={"character_id": "rin", "content": f"隔离验收动态 {index}：仓库示例插画，仅用于布局验证。", "media_ids": [mixed_server.media_id]})
        assert result.ok
    page.goto(base + "/?view=space")
    expect(page.locator(".space-topics-rail h2").first).to_have_text("热点讨论")
    expect(page.locator(".space-topics-content .space-external-card h2")).to_have_text("隔离演示话题")
    page.wait_for_function("document.querySelectorAll('.space-feed [data-space-entry]').length >= 14")
    images = page.locator('.space-feed [data-space-post] img')
    images.evaluate_all("images => Promise.all(images.map(image => image.decode()))")
    assert images.count() >= 7
    assert page.locator('.space-topics-rail .cm-social-rail').count() == 1
    assert page.locator('.space-shell > .cm-social-rail').count() == 0
    image_metrics = images.evaluate_all("images => images.map(image => ({naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight,width:image.width,height:image.height}))")
    for metric in image_metrics:
        assert abs(metric['width'] / metric['height'] - metric['naturalWidth'] / metric['naturalHeight']) < .02
    assert page.locator(".space-feed").evaluate("el => +getComputedStyle(el).columnCount") == (2 if width == 1440 else 3)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    feed_box = page.locator(".space-feed").bounding_box()
    rail_box = page.locator(".space-topics-rail").bounding_box()
    assert rail_box["x"] >= feed_box["x"] + feed_box["width"] + 10
    output = os.getenv("DESKTOP_UI_EVIDENCE_DIR")
    folder = Path(output) if output else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f"space-{width}x{height}.png"), animations='disabled')
    page.locator(".ensemble-create-button").click()
    expect(page.locator(".ensemble-builder-desktop")).to_be_visible()
    drawer_box = settled_drawer(page)
    assert_inside_viewport(drawer_box, width, height)
    action = page.locator("[data-ensemble-start]")
    textarea = page.locator("[data-ensemble-prompt]")
    expect(action).to_be_visible()
    assert textarea.bounding_box()["height"] == 120
    assert_inside_viewport(action.bounding_box(), width, height)
    assert not page.locator(".ensemble-voice-details").evaluate("el => el.open")
    expect(page.locator(".ensemble-capacity-note")).to_contain_text("开启私聊后才计入")
    if folder:
        page.screenshot(path=str(folder / f"builder-{width}x{height}.png"), animations='disabled')
    action.click()
    expect(page.locator("[data-ensemble-error]")).to_contain_text("先写一句")
    if folder:
        page.screenshot(path=str(folder / f"builder-error-{width}x{height}.png"), animations='disabled')
    page.locator(".ensemble-voice-details summary").click()
    page.locator("[data-ensemble-voice-design]").check()
    page.locator("[data-ensemble-mode]").select_option("SCENE")
    expect(textarea).to_be_hidden()
    expect(page.locator("[data-ensemble-scene-background]")).to_be_visible()
    page.locator("[data-ensemble-mode]").select_option("SOURCE")
    page.locator("[data-ensemble-template]").click()
    expect(page.locator(".ensemble-confirmation")).to_be_visible()
    expect(page.locator("[data-ensemble-member]")).to_have_count(4)
    expect(page.locator("[data-ensemble-capacity]")).to_contain_text("群成员不占角色名额")
    confirm_drawer = settled_drawer(page)
    assert_inside_viewport(confirm_drawer, width, height)
    confirmation_actions = page.locator('[data-ensemble-confirm]')
    confirmation_actions.scroll_into_view_if_needed()
    assert_inside_viewport(confirmation_actions.bounding_box(), width, height)
    if folder:
        page.screenshot(path=str(folder / f"confirmation-{width}x{height}.png"), animations='disabled')
        root = Path(__file__).resolve().parents[1]
        paths = ['src/character_memory/web/' + name for name in ('space.js', 'space.css', 'ensemble.js', 'ensemble.css')]
        source_hashes = {path:hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths}
        source_dirty = bool(subprocess.check_output(['git', 'status', '--porcelain', '--', *paths], cwd=root, text=True).strip())
        (folder / f"desktop-metrics-{width}.json").write_text(json.dumps({"viewport": {"width": width, "height": height}, "feed": feed_box, "topics": rail_box, "drawer":drawer_box, "confirmation_drawer":confirm_drawer, "image_metrics":image_metrics, "result": "PASS", "fixture": "isolated deterministic app; repository-owned illustration, not real character experience", "base_commit":subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(), "source_dirty":source_dirty, "source_sha256":source_hashes, "browser":page.context.browser.version, "captured_at":datetime.now(timezone.utc).isoformat(), "human_visual_signoff":"PENDING"}, ensure_ascii=False, indent=2), encoding="utf-8")
    page.locator("[data-ensemble-discard]").click()
    expect(page.locator(".drawer.open")).to_have_count(0)


def test_mobile_builder_retains_existing_form(page, mixed_server):
    from playwright.sync_api import expect

    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(mixed_server[0] + "/?view=space")
    page.wait_for_function("window.CM && document.body.dataset.cmReady === 'true'")
    page.evaluate("window.CM.features.ensemble.open()")
    expect(page.locator("[data-ensemble-start]")).to_be_visible()
    expect(page.locator(".ensemble-builder-desktop")).to_have_count(0)
    expect(page.locator(".ensemble-voice-details")).to_have_count(0)
    expect(page.locator(".space-topics-rail")).to_be_hidden()
