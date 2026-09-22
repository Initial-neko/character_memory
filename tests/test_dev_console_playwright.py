"""The rendered Dev Console opens on the short list.

``tests/test_dev_console_levels.py`` checks the contract in the markup; this
checks the page the browser builds from it, because the two are not the same
claim. The markup could declare every level and still put a control on the first
screen -- dev_levels.js could have failed to run, a group could have been left
open, or the sweep could have moved something back out into view.

Two things about the Dev Console are only true of the rendered page:

* what a person can see without clicking (``common``, and only ``common``);
* that nothing needed the runtime sweep, i.e. the markup said where every
  control belongs rather than leaving it to be rescued.

The console is started in this process on a private port: the page's own probes
go to the real :8000/:8001, and this test does not care whether they answer --
the status badges are not what is being measured.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest


pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="browser smoke runs in dedicated CI job"),
]

ROOT = Path(__file__).resolve().parents[1]

# The first screen, by id, matching tests/test_dev_console_levels.py.
FIRST_SCREEN = [
    "refreshAll",
    "llmPrompt",
    "runLlm",
    "ttsText",
    "runTts",
    "spaceEnabled",
    "spaceCharacter",
    "applySpaceConfig",
    "runSpaceOpportunity",
    "groupAutonomyEnabled",
    "groupAutonomyGroup",
    "applyGroupAutonomyConfig",
    "runGroupAutonomyOpportunity",
]

# The page this replaced was 6249px tall and exposed 87 controls at rest, every
# one of them a live field. The bound is loose on purpose -- fonts and wrapping
# differ between machines -- and only fires if the levels stopped being applied.
TALLEST_ACCEPTABLE_PAGE = 3000

_VISIBLE_CONTROLS = """() => [...document.querySelectorAll('button, input, select, textarea')]
  .filter(el => el.checkVisibility({contentVisibilityAuto: true, visibilityProperty: true, opacityProperty: true}))
  .map(el => el.id)"""

_ABOVE_THE_FOLD = """() => [...document.querySelectorAll('button, input, select, textarea')]
  .filter(el => el.checkVisibility({contentVisibilityAuto: true, visibilityProperty: true, opacityProperty: true}))
  .filter(el => el.getBoundingClientRect().top < window.innerHeight)
  .map(el => el.id)"""

_GROUPS = """() => [...document.querySelectorAll('details.level-group')].map(details => ({
  level: details.dataset.level,
  hint: details.dataset.hint || '',
  controls: details.querySelectorAll('button, input, select, textarea').length,
  summary: details.querySelector(':scope > summary').textContent,
}))"""


@pytest.fixture(scope="module")
def dev_console_url():
    """The Dev Console served from this checkout, on a port nothing else wants."""

    uvicorn = pytest.importorskip("uvicorn")
    from character_memory.dev_server import create_dev_app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    server = uvicorn.Server(
        uvicorn.Config(
            create_dev_app(str(ROOT / "config.example.yaml")),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("the Dev Console did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    worker.join(timeout=6)


def _load(page, dev_console_url: str) -> None:
    """Open /dev and wait for dev_levels.js to finish naming the groups.

    The summary rewrite is synchronous once the script runs, so the counted
    text is the page's own "loaded" signal rather than a sleep.
    """

    page.goto(f"{dev_console_url}/dev", wait_until="domcontentloaded")
    page.wait_for_selector("details.level-group > summary")
    page.wait_for_function(
        "() => { const summary = document.querySelector('details.level-group[data-level=\"advanced\"] > summary');"
        " return !!summary && summary.textContent.includes('项）'); }"
    )


def test_the_rendered_page_exposes_only_the_first_screen(dev_console_url):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _load(page, dev_console_url)

        # Every control a person can reach without opening anything.
        assert page.evaluate(_VISIBLE_CONTROLS) == FIRST_SCREEN

        # And they are on the first screen, not merely unfolded: the point of
        # the levels is that the answer to "where do I start" is one screenful.
        assert page.evaluate(_ABOVE_THE_FOLD) == FIRST_SCREEN

        # Nothing arrives expanded.
        assert page.locator("details[open]").count() == 0
        assert page.evaluate("() => document.body.scrollHeight") < TALLEST_ACCEPTABLE_PAGE

        # A closed group has to be worth opening: the summary counts what it
        # holds and names the first few, so it cannot say the wrong thing about
        # its contents. The one group that holds prose rather than controls
        # (the extension-slot note) claims no count.
        groups = page.evaluate(_GROUPS)
        assert groups
        for group in groups:
            summary = group["summary"]
            assert group["hint"], group
            if group["controls"]:
                assert f"（{group['controls']} 项）" in summary, summary
                assert "：" in summary, summary
            else:
                assert "项）" not in summary, summary

        browser.close()


def test_the_markup_was_complete_enough_that_nothing_needed_rescuing(dev_console_url):
    """`sweep` is the runtime default for a control that declares no level.

    It is a safety net, not a feature: on a page whose markup says where every
    control belongs it finds nothing. A non-empty list here means a control was
    added without a level and is being kept off the first screen by the sweep
    instead of by its author -- the same failure
    ``test_every_control_declares_a_level`` reports statically.
    """

    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _load(page, dev_console_url)
        assert page.evaluate("() => window.CMDevLevels.lastSweep") == []
        browser.close()
