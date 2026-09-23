"""What the browser does with each of the three voice-registry outcomes.

``tests/test_character_archive.py`` pins the payloads the route can return.
Whether the user is told anything, and whether they can get rid of what they are
told, is a different claim about a different file: ``character_archive.js`` is an
IIFE that writes DOM, and what this replaces was ``assert "reloaded !== false" in
script`` -- a string that is just as true of a module that renders nothing.

So the real module runs against a real DOM in a real browser. The payloads are
not written here either: they are requested from the route itself, through the
helpers in ``test_character_archive.py``, against a fake sidecar on a real port,
so a field renamed on the API side fails here rather than being papered over by a
hand-written dict.

The bug these cover, in one line: every archive on a deployment with no GSV
sidecar used to pin a banner to ``top:84px`` for the rest of the session, saying
a running sidecar might still be synthesizing the old roster -- on a machine
that had no sidecar to synthesize with -- and it could not be dismissed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from test_character_archive import (
    _fake_sidecar,
    _voice_registry_after_archive,
)


pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="browser smoke runs in dedicated CI job"),
]

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"

NOTICE = ".archive-voice-notice"
DISMISS = "[data-archive-voice-dismiss]"

# The window the transient notice clears itself in, plus room for a loaded
# machine. Read off the behaviour, not off the constant: a test that imported
# the number would pass if the module stopped honouring it.
TRANSIENT_CLEAR_MS = 15000

_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>archive</title></head>
<body>
  <div id="characterActions"></div>
  <div class="topbar-actions"></div>
  <div id="characterList"></div>
  <div id="drawerBody"></div>
  <script>
  window.CM = {
    state: {
      characters: [{id:"rin", name:"Rin"}, {id:"momo", name:"Momo"}],
      characterId: "rin",
      characterCapacity: {activeTotal:2, softLimit:10, hardLimit:20},
    },
    features: {},
    dom: {},
    api: async () => ({}),
    on: () => {},
    registerFeature: (name, api) => { window.CM.features[name] = api; },
    isGroupConversation: () => false,
    loadCharacters: async () => {},
    switchCharacter: async () => {},
    openDrawer: () => {},
    closeDrawer: () => { window.__drawerCloses = (window.__drawerCloses || 0) + 1; },
    fmtDate: value => String(value),
    escapeHtml: value => String(value).replace(/[&<>"']/g, ch => (
      {"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[ch]
    )),
  };
  window.CM.dom.characterList = document.getElementById("characterList");
  window.CM.dom.drawerBody = document.getElementById("drawerBody");
  </script>
</body></html>"""


@pytest.fixture
def registry_payloads(tmp_path: Path, monkeypatch) -> dict[str, dict]:
    """The three outcomes, produced by the route rather than by this file."""

    payloads: dict[str, dict] = {}
    with _fake_sidecar(status=200) as url:
        payloads["reloaded"] = _voice_registry_after_archive(_scratch(tmp_path, "reloaded"), monkeypatch, url)
    with _fake_sidecar(status=400, detail="voices/momo.yaml: template 'murasame' is not registered") as url:
        payloads["rejected"] = _voice_registry_after_archive(_scratch(tmp_path, "rejected"), monkeypatch, url)
    # 502 rather than a dead port: a proxy in TUN mode answers a closed loopback
    # port with one, which is what the real deployments without a sidecar see,
    # and it costs no timeout to obtain.
    with _fake_sidecar(status=502) as url:
        payloads["unreachable"] = _voice_registry_after_archive(_scratch(tmp_path, "unreachable"), monkeypatch, url)
    assert [payload["status"] for payload in payloads.values()] == ["reloaded", "rejected", "unreachable"]
    return payloads


def _scratch(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


def _open(page) -> None:
    page.set_content(_PAGE, wait_until="domcontentloaded")
    page.add_style_tag(path=str(WEB / "styles.css"))
    page.add_script_tag(path=str(WEB / "character_archive.js"))


def _archive(page, payload: dict) -> None:
    """Run the real archive flow with the route's own response."""

    page.evaluate(
        """async payload => {
             window.CM.api = async () => payload;
             await window.CM.features.characterArchive.archiveCharacter("momo");
           }""",
        payload,
    )


def _notice_state(page) -> dict:
    return page.evaluate(
        """() => {
             const notice = document.querySelector('%s');
             const dismiss = notice.querySelector('%s');
             return {
               hidden: notice.classList.contains('hidden'),
               text: notice.textContent.trim(),
               canvasPointerEvents: getComputedStyle(notice).pointerEvents,
               dismissPointerEvents: dismiss ? getComputedStyle(dismiss).pointerEvents : null,
               hasDismiss: !!dismiss,
               drawerCloses: window.__drawerCloses || 0,
             };
           }"""
        % (NOTICE, DISMISS)
    )


def test_a_clean_reload_leaves_nothing_on_screen(registry_payloads):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page()
        _open(page)
        _archive(page, {"voice_registry": registry_payloads["reloaded"]})

        state = _notice_state(page)
        assert state["hidden"] is True
        assert state["text"] == ""
        assert state["drawerCloses"] == 1, "the drawer closes on every path, warning or not"
        browser.close()


def test_a_refused_reload_warns_truthfully_and_can_be_dismissed(registry_payloads):
    """The sidecar answered, so the warning is allowed to say a sidecar is running.

    And it has to be gettable-rid-of: the drawer is already closed, so a notice
    with no dismiss and no timer is furniture for the rest of the session.
    """

    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page()
        _open(page)
        _archive(page, {"voice_registry": registry_payloads["rejected"]})

        state = _notice_state(page)
        assert state["hidden"] is False
        assert "sidecar" in state["text"], state["text"]
        assert "murasame" in state["text"], "the sidecar's own sentence is the actionable part"
        assert state["drawerCloses"] == 1, "a failed refresh must not hold the drawer open"

        # The strip must not stand between the user and the sidebar: it stays
        # transparent to the pointer and only the button opts back in. This is
        # the property that a Playwright click on `.character-archive-entry`
        # depends on.
        assert state["canvasPointerEvents"] == "none"
        assert state["dismissPointerEvents"] == "auto"
        assert state["hasDismiss"] is True

        # It survives the flow that produced it -- this is the standing warning,
        # not the transient one -- so it cannot be a race that makes the
        # dismissal below look like it worked.
        page.wait_for_timeout(1500)
        assert _notice_state(page)["hidden"] is False

        page.click(DISMISS)
        state = _notice_state(page)
        assert state["hidden"] is True
        assert state["text"] == ""
        browser.close()


def test_an_unreachable_sidecar_never_gets_a_standing_banner(registry_payloads):
    """The reported bug, as the user experiences it.

    No sidecar is running. Whatever the module chooses to say -- a note that
    clears itself, or nothing at all -- the one thing it may not do is leave a
    banner pinned over the page that only the *next* clean archive would clear,
    because on this deployment there is no next clean archive. And it may not
    claim a sidecar is doing anything: there is none.
    """

    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page()
        _open(page)
        _archive(page, {"voice_registry": registry_payloads["unreachable"]})

        state = _notice_state(page)
        assert state["drawerCloses"] == 1
        assert state["canvasPointerEvents"] == "none"

        if not state["hidden"]:
            assert "sidecar" not in state["text"], (
                "there is no sidecar running, so nothing may say one is synthesizing: " + state["text"]
            )
            # Gone on its own, with nobody clicking anything.
            page.wait_for_selector(NOTICE, state="hidden", timeout=TRANSIENT_CLEAR_MS)
            assert _notice_state(page)["hidden"] is True

        # And archiving again in the same session does not bring back the strip
        # that could only be cleared by a clean reload -- the one this module
        # used to leave parked over the sidebar for the rest of the session.
        _archive(page, {"voice_registry": registry_payloads["unreachable"]})
        state = _notice_state(page)
        assert state["hidden"] or state["hasDismiss"], state
        browser.close()


def test_a_response_without_a_status_still_warns():
    """An API older than this fix can only over-report, never go silent.

    Every payload from that version meant "the refresh did not happen", because
    that version had no way to say anything else.
    """

    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page()
        _open(page)
        _archive(page, {"voice_registry": {"ok": False, "reloaded": False, "reason": "boom"}})

        state = _notice_state(page)
        assert state["hidden"] is False
        assert "sidecar" in state["text"]
        assert state["hasDismiss"] is True
        browser.close()
