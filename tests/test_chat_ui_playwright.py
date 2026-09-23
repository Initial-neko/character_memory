"""The chat surface's four measured UI contracts, in a real browser.

Every one of these shipped broken and none of them is visible from the source:

* the two composer popovers (the "☺" sticker panel and the "＋" tools menu)
  could be open at the same time and stacked on top of each other, because each
  trigger stops propagation and so neither trigger's click ever reached the
  other's document-level closer;
* the sticker pack strip was one `overflow-x: auto` row in a 408px box, so
  packs past the fifth were clipped by the panel's `overflow: hidden` with no
  scrollbar and no gesture to find them;
* the "想法" and "···" buttons under each assistant message measured 28x14 and
  22x14 -- under half of WCAG 2.5.8's 24x24 minimum, on a phone-sized screen;
* the timestamps those buttons sit next to were 2.53:1 on white.

``ui.css``'s tokens and ``styles.css``'s rules are asserted statically in
test_ui_baseline.py; this file asserts what the browser actually paints, on a
throwaway copy of the app (its own DB, its own persona and sticker library), so
a stylesheet that is loaded after these rules -- or a JS file that stops a click
-- cannot make the static assertions pass while the page is still broken.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

if os.getenv("RUN_PLAYWRIGHT") == "1":
    from playwright.sync_api import sync_playwright
else:
    sync_playwright = None

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="browser smoke runs in dedicated CI job"),
]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STICKERS = ROOT / "src" / "character_memory" / "web" / "stickers" / "default"

# Eight packs is what the report measured: with the built-in one that is nine
# tabs, comfortably past the 408px the strip has to hold.
PACKS = ["基础情绪包", "甜蜜撒娇包", "炸毛生气包", "困惑思考包", "慵懒睡眠包", "加班打工包", "周末出门包"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_stickers(root: Path) -> None:
    """A global sticker library with more packs than the strip can hold."""

    sticker_dir = root / "e2e-stickers"
    sticker_dir.mkdir(parents=True, exist_ok=True)
    sources = sorted(DEFAULT_STICKERS.glob("*.svg"))
    lines = ["stickers:"]
    for index, name in enumerate(PACKS):
        source = sources[index % len(sources)]
        file_name = f"pack{index}.svg"
        shutil.copyfile(source, sticker_dir / file_name)
        lines += [
            f"  - id: pack{index}",
            f"    file: {file_name}",
            f"    label: {name}·一",
            f"    tags: [测试]",
            f"    pack_id: pack{index}",
            f"    pack_name: {name}",
        ]
    (sticker_dir / "manifest.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_persona(root: Path) -> None:
    """One character, from the repo's own persona, without its sticker folder.

    The default persona root is the working tree's ``personas/`` -- a developer
    with an elaborate untracked library would otherwise change how many tabs
    this test sees.
    """

    target = root / "personas" / "rin"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "personas" / "rin" / "persona.yaml", target / "persona.yaml")


@pytest.fixture(scope="module")
def chat_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("chat-ui")
    _write_stickers(root)
    _write_persona(root)
    port = _free_port()
    env = os.environ.copy()
    env["CHARACTER_MEMORY_E2E_DB"] = str(root / "e2e.db")
    env["CHARACTER_MEMORY_E2E_MEDIA"] = str(root / "e2e-media")
    env["CHARACTER_MEMORY_E2E_PERSONA_ROOT"] = str(root / "personas")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "e2e_realtime_app:app",
            "--app-dir",
            str(ROOT / "tests"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"chat UI server exited early:\n{output}")
        try:
            with urlopen(f"{base}/health", timeout=0.5) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        process.terminate()
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError(f"chat UI server did not become ready:\n{output}")

    yield base

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture(scope="module")
def page(chat_server):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium")
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(f"{chat_server}/", wait_until="networkidle")
        page.wait_for_selector(".composer-tools-trigger", timeout=20000)
        page.wait_for_selector(".sticker-trigger", timeout=20000)
        page.set_default_timeout(15000)
        yield page
        browser.close()


def _open(page, selector: str) -> None:
    """Click a composer trigger and wait for the click to be handled.

    Each trigger stops propagation, so Playwright's actionability check cannot
    be what makes these waits deterministic: the panel is what has to settle.
    """

    page.click(selector)
    page.wait_for_timeout(400)


def _popovers(page) -> dict:
    return page.evaluate(
        """() => {
          const box = (sel) => {
            const el = document.querySelector(sel);
            if (!el) return {shown: false};
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return {shown: r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden',
                    x: r.x, y: r.y, right: r.right, bottom: r.bottom};
          };
          const sticker = box('.sticker-panel');
          const tools = box('.composer-tools-menu');
          const overlap = sticker.shown && tools.shown
            && Math.min(sticker.right, tools.right) > Math.max(sticker.x, tools.x)
            && Math.min(sticker.bottom, tools.bottom) > Math.max(sticker.y, tools.y);
          return {sticker: sticker.shown, tools: tools.shown, overlap: Boolean(overlap),
                  stickerExpanded: document.querySelector('.sticker-trigger').getAttribute('aria-expanded'),
                  toolsExpanded: document.querySelector('.composer-tools-trigger').getAttribute('aria-expanded')};
        }"""
    )


def _close_popovers(page) -> None:
    page.keyboard.press("Escape")
    page.mouse.click(720, 300)
    page.wait_for_timeout(300)


def test_the_two_composer_popovers_never_stack(page):
    """Opening one closes the other, in both orders and from either trigger."""

    _close_popovers(page)

    _open(page, ".sticker-trigger")
    assert _popovers(page) == {"sticker": True, "tools": False, "overlap": False,
                              "stickerExpanded": "true", "toolsExpanded": "false"}

    _open(page, ".composer-tools-trigger")
    state = _popovers(page)
    assert state["tools"] is True, "the tools menu has to open"
    assert state["sticker"] is False, "the sticker panel is still behind it"
    assert state["overlap"] is False, "two floating panels painted on top of each other"
    assert state["stickerExpanded"] == "false"

    _open(page, ".sticker-trigger")
    state = _popovers(page)
    assert state["sticker"] is True
    assert state["tools"] is False, "the tools menu stayed open under the sticker panel"
    assert state["toolsExpanded"] == "false"

    _close_popovers(page)
    assert _popovers(page) == {"sticker": False, "tools": False, "overlap": False,
                              "stickerExpanded": "false", "toolsExpanded": "false"}


def test_every_sticker_pack_tab_is_inside_the_panel(page):
    """A pack nobody can see is a pack nobody can pick."""

    _close_popovers(page)
    _open(page, ".sticker-trigger")
    page.wait_for_selector(".sticker-pack-tab", timeout=15000)

    report = page.evaluate(
        """() => {
          const panel = document.querySelector('.sticker-panel');
          const strip = document.querySelector('.sticker-pack-tabs');
          const rect = panel.getBoundingClientRect();
          const tabs = [...strip.querySelectorAll('.sticker-pack-tab')];
          const outside = tabs
            .filter(el => { const r = el.getBoundingClientRect();
              return r.width === 0 || r.left < rect.left - 0.5 || r.right > rect.right + 0.5; })
            .map(el => el.textContent.trim());
          return {count: tabs.length, outside,
                  hidesSomething: panel.scrollWidth > panel.clientWidth + 1,
                  stripScrollsSideways: strip.scrollWidth > strip.clientWidth + 1};
        }"""
    )

    assert report["count"] == len(PACKS) + 1, "one tab per pack (plus the built-in one) must render"
    assert report["outside"] == [], f"packs clipped out of the panel: {report['outside']}"
    assert report["hidesSomething"] is False
    assert report["stripScrollsSideways"] is False, "the strip must not need a sideways scroll"
    _close_popovers(page)


def _ensure_assistant_message(page) -> None:
    """One exchange through the real composer: the meta row only exists once the
    character has answered, and the buttons only exist on that answer."""

    if page.locator(".message-row.assistant .detail-button").count():
        return
    page.fill(".composer textarea", "晚上好")
    page.get_by_role("button", name="发送", exact=True).click()
    page.wait_for_selector(".message-row.assistant .detail-button", timeout=25000)


def test_message_meta_buttons_meet_the_touch_target_minimum(page):
    """24x24 at the 10px those two buttons are set in, without moving the row."""

    _ensure_assistant_message(page)

    report = page.evaluate(
        """() => {
          const row = document.querySelector('.message-row.assistant .message-meta');
          const buttons = [...row.querySelectorAll('.detail-button')].map(el => {
            const r = el.getBoundingClientRect();
            return {text: el.textContent.trim(), width: r.width, height: r.height, left: r.left, right: r.right};
          });
          return {rowHeight: row.getBoundingClientRect().height, buttons};
        }"""
    )

    assert len(report["buttons"]) >= 2, "the assistant row keeps its 想法 and ··· buttons"
    for button in report["buttons"]:
        assert button["width"] >= 24, f"{button['text']} is {button['width']:.1f}px wide"
        assert button["height"] >= 24, f"{button['text']} is {button['height']:.1f}px tall"
    # The padding that makes the target is given back as margin, so the row
    # still measures the 14px line it did before -- the transcript's rhythm is
    # not what this fix is allowed to spend.
    assert report["rowHeight"] <= 15, f"the meta row grew to {report['rowHeight']:.1f}px"

    ordered = sorted(report["buttons"], key=lambda item: item["left"])
    for first, second in zip(ordered, ordered[1:]):
        assert first["right"] <= second["left"], "the grown targets overlap each other"


def test_message_meta_text_clears_wcag_aa(page):
    """The timestamps the browser paints, not the token the stylesheet declares."""

    _ensure_assistant_message(page)
    values = page.evaluate(
        """() => {
          const parse = (value) => value.match(/rgba?\\(([^)]+)\\)/)[1].split(',').map(Number);
          const meta = document.querySelector('.message-row.assistant .message-meta');
          let node = meta;
          while (node) {
            const colour = getComputedStyle(node).backgroundColor;
            const parts = parse(colour);
            if ((parts[3] ?? 1) === 1) return {text: parse(getComputedStyle(meta).color), background: parts};
            node = node.parentElement;
          }
          return {text: parse(getComputedStyle(meta).color), background: [255, 255, 255]};
        }"""
    )

    def luminance(rgb):
        def channel(value):
            value = value / 255
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

        red, green, blue = (channel(value) for value in rgb[:3])
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    high, low = sorted((luminance(values["text"]), luminance(values["background"])), reverse=True)
    ratio = (high + 0.05) / (low + 0.05)
    assert ratio >= 4.5, f"the timestamp renders at {ratio:.2f}:1"
