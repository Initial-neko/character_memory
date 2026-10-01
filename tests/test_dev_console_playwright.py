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
#
# The video controls are here on purpose. They used to sit behind both folding
# surfaces -- "detailed" mode and a collapsed diagnostic group -- and a paid
# capability whose check cannot be seen is a check that never runs, so the two
# video checks were pulled out into a card of their own. That is the one
# deliberate exception to "the first screen is only the entry points": the card
# is the entry point for a capability that costs money, and the whole point of
# the split is that a person can tell a broken provider from a Space-layer hold
# without unfolding anything.
FIRST_SCREEN = [
    "refreshAll",
    "devModeToggle",
    "spaceCharacter",
    "runSpaceOpportunity",
    "videoSmokePrompt",
    "videoSmokeResolution",
    "runVideoSmoke",
    "videoSmokeDryRun",
    "videoPostPrompt",
    "videoPostText",
    "runVideoPost",
    "groupAutonomyGroup",
    "runGroupAutonomyOpportunity",
]

# The page this replaced was 6249px tall and exposed 87 controls at rest, every
# one of them a live field. The bound is loose on purpose -- fonts and wrapping
# differ between machines -- and only fires if the levels stopped being applied.
# It grew with the video card, which is above every folding surface by design.
TALLEST_ACCEPTABLE_PAGE = 3600

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
    page.locator("details.level-group > summary").first.wait_for(state="attached")
    page.wait_for_function(
        "() => { const summary = document.querySelector('details.level-group[data-level=\"advanced\"] > summary');"
        " return !!summary && summary.textContent.includes('项）'); }"
    )


def test_the_rendered_page_exposes_only_the_first_screen(page, dev_console_url):
    page.set_viewport_size({"width": 1440, "height": 900})
    _load(page, dev_console_url)

    # Every control a person can reach without opening anything.
    assert page.evaluate(_VISIBLE_CONTROLS) == FIRST_SCREEN

    # And they are reachable without opening anything, not merely present in
    # the DOM. The entry points specifically are the answer to "where do I
    # start", so those have to be on the first screenful; the video card may
    # start lower, which is why this is a containment check rather than
    # equality -- unfolding is what it must never require.
    above_the_fold = page.evaluate(_ABOVE_THE_FOLD)
    assert set(above_the_fold) <= set(FIRST_SCREEN)
    for entry_point in ("refreshAll", "devModeToggle", "spaceCharacter", "runSpaceOpportunity"):
        assert entry_point in above_the_fold

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


def test_the_markup_was_complete_enough_that_nothing_needed_rescuing(page, dev_console_url):
    """The runtime sweep is a safety net, not the ownership model.

    A non-empty list here means a control was added without a declared level
    and only hidden at runtime instead of by its author.
    """

    page.set_viewport_size({"width": 1440, "height": 900})
    _load(page, dev_console_url)
    assert page.evaluate("() => window.CMDevLevels.lastSweep") == []


def test_detailed_mode_reveals_detailed_tools_without_changing_first_screen_contract(page, dev_console_url):
    page.set_viewport_size({"width": 1440, "height": 900})
    _load(page, dev_console_url)

    body = page.locator("body")
    assert body.get_attribute("data-dev-mode") == "simple"
    assert page.locator("#llmSmokeCard").evaluate("(el) => getComputedStyle(el).display") == "none"

    page.locator("#devModeToggle").click()
    assert body.get_attribute("data-dev-mode") == "detailed"
    assert page.locator("#llmSmokeCard").evaluate("(el) => getComputedStyle(el).display") != "none"

    page.locator("#devModeToggle").click()
    assert body.get_attribute("data-dev-mode") == "simple"
    assert page.locator("#llmSmokeCard").evaluate("(el) => getComputedStyle(el).display") == "none"


def test_space_runtime_override_is_rendered_as_temporary_not_persistent_config(page, dev_console_url):
    page.set_viewport_size({"width": 1440, "height": 900})
    _load(page, dev_console_url)

    # Runtime override controls intentionally live behind Detailed Dev;
    # reveal that surface the same way an operator does before opening the group.
    page.locator("#devModeToggle").click()
    assert page.locator("body").get_attribute("data-dev-mode") == "detailed"

    group = page.locator("details:has(#spaceIntervalMinutes)").first
    assert group.get_attribute("open") is None
    group.locator("summary").click()

    page.locator("#spaceIntervalMinutes").wait_for(state="visible")
    text = group.inner_text()
    assert "Session Override（仅当前 Runtime）" in text
    assert "不写 config.yaml" in text
    assert "正式配置在 Settings" in text
