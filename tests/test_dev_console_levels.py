"""The Dev Console opens on a short list, and the list is the one we chose.

The Settings Center gets its levels from ``field_level()``: every schema field
carries one, and a field that forgets lands in ``diagnostic`` so forgetting
under-exposes instead of cluttering the first screen. The Dev Console has no
schema -- its controls are hand-written markup -- so the same contract is
carried by the DOM instead: a block declares ``data-level``, ``dev_levels.js``
resolves a control to its nearest declaring ancestor, and a control with no
declaring ancestor is ``diagnostic``.

That leaves two ways the contract can rot, and each has a test here:

* the markup stops declaring levels, so the page depends on the runtime sweep
  to stay tidy -- caught by requiring every control to declare one;
* a control is quietly promoted onto the first screen -- caught by naming the
  first screen instead of counting it, so adding to it is an edit here too.

The page the browser actually builds is checked in
``tests/test_dev_console_playwright.py``; this file is the static half and runs
without a browser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"

LEVELS = ("common", "advanced", "diagnostic")
DEFAULT_LEVEL = "diagnostic"
CONTROLS = {"button", "input", "select", "textarea"}
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}

# The first screen, by id. Named rather than counted because "10-15 controls" is
# not the contract -- *these* controls being reachable without a click is. A
# control added without a level resolves to `diagnostic` and cannot join this set
# by accident; joining it takes an edit on both sides of the contract.
FIRST_SCREEN = {
    "refreshAll",
    "devModeToggle",
    "spaceEnabled",
    "spaceCharacter",
    "spaceIntervalMinutes",
    "spaceMaxPostsPerDay",
    "applySpaceConfig",
    "runSpaceOpportunity",
    "groupAutonomyEnabled",
    "groupAutonomyGroup",
    "groupAutonomyInterval",
    "applyGroupAutonomyConfig",
    "runGroupAutonomyOpportunity",
    "llmUsageWindow",
    "refreshLlmUsage",
}
# Simple Dev is the operating surface. Provider smoke and low-level diagnostic
# controls can still declare their internal level, but a detailed-only card is
# outside this band until the user explicitly switches modes.
FIRST_SCREEN_BAND = (10, 16)


class _Markup(HTMLParser):
    """The page as a flat list of controls and groups, each with its ancestors.

    Per control: its id, the nearest ancestor that declares a level (``None``
    when nothing does -- the case the runtime sweeps), and how many ``<details>``
    it sits inside. Per group: its attributes, whether a ``<summary>`` sits
    directly inside it, and how many controls it holds.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None, str | None]] = []
        self.controls: list[dict] = []
        self.groups: list[dict] = []
        self._open_groups: list[int] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs or {})
        if tag in CONTROLS:
            self.controls.append(
                {
                    "tag": tag,
                    "id": attrs.get("id"),
                    "declared": self._declared(),
                    "surface": self._surface(),
                    "details": sum(1 for name, _, _ in self.stack if name == "details"),
                }
            )
            for index in self._open_groups:
                self.groups[index]["controls"] += 1
        elif tag == "details":
            self.groups.append({"attrs": attrs, "summary": False, "controls": 0})
            self._open_groups.append(len(self.groups) - 1)
        elif tag == "summary" and self._open_groups and self.stack and self.stack[-1][0] == "details":
            self.groups[self._open_groups[-1]]["summary"] = True

        if tag not in _VOID:
            self.stack.append((tag, attrs.get("data-level"), attrs.get("data-dev-surface")))

    def handle_endtag(self, tag):
        if tag == "details" and self._open_groups:
            self._open_groups.pop()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def _declared(self) -> str | None:
        for _tag, level, _surface in reversed(self.stack):
            if level is not None:
                return level
        return None

    def _surface(self) -> str | None:
        for _tag, _level, surface in reversed(self.stack):
            if surface is not None:
                return surface
        return None


def _page() -> _Markup:
    parsed = _Markup()
    parsed.feed((WEB / "dev.html").read_text(encoding="utf-8"))
    parsed.close()
    return parsed


def _label(control: dict) -> str:
    return control["id"] or control["tag"]


def _is_level_group(group: dict) -> bool:
    return "level-group" in (group["attrs"].get("class") or "").split()


def test_every_control_declares_a_level():
    """An undeclared control is swept into `diagnostic` -- so this is the proof
    that the markup does not depend on the rescue. Failing here means a control
    was added without saying where it belongs, and the page is being kept tidy
    by the runtime instead of by its author."""

    undeclared = [_label(control) for control in _page().controls if control["declared"] is None]
    assert undeclared == []


def test_a_declared_level_is_one_of_the_three_levels():
    """Anything else resolves to `diagnostic` at runtime, which makes a typo a
    silent demotion. `common` spelled wrong would move a control *off* the first
    screen -- say so here instead of leaving it to be noticed."""

    parsed = _page()
    unknown = sorted({c["declared"] for c in parsed.controls if c["declared"] not in LEVELS})
    assert unknown == []


def test_the_first_screen_is_the_named_short_list():
    parsed = _page()
    shown = [
        control for control in parsed.controls
        if control["declared"] == "common" and control["surface"] != "detailed"
    ]

    assert sorted(_label(control) for control in shown) == sorted(FIRST_SCREEN)
    low, high = FIRST_SCREEN_BAND
    assert low <= len(shown) <= high, f"{len(shown)} controls on the first screen"

    # `common` is by definition not behind a click, so none of it may sit inside
    # a group -- an unopened <details> is the one thing a control can hide in.
    assert [_label(control) for control in shown if control["details"]] == []


def test_nothing_outside_the_first_screen_is_exposed():
    """The other half of the band: every non-common control is inside at least
    one group. The raw-payload blocks hold no controls, so a control that fails
    this is one a user reads without asking for it."""

    parsed = _page()
    exposed = [
        _label(c) for c in parsed.controls
        if not (c["declared"] == "common" and c["surface"] != "detailed")
        and c["surface"] != "detailed"
        and not c["details"]
    ]
    assert exposed == []


def test_both_hidden_levels_carry_real_controls():
    """Each level earns its name: `advanced` is what needs a decision and
    `diagnostic` is what needs a reason. A level with nothing in it is a name
    the page claims but does not use."""

    counts = Counter(control["declared"] for control in _page().controls)
    assert counts["advanced"] > 0
    assert counts["diagnostic"] > 0


def test_every_group_is_closed_and_has_something_to_click():
    parsed = _page()
    assert parsed.groups
    for group in parsed.groups:
        attrs = group["attrs"]
        where = attrs.get("data-title") or attrs.get("class") or attrs.get("id") or "<details>"
        assert "open" not in attrs, f"{where} starts expanded"
        assert group["summary"], f"{where} has no <summary>, so it cannot be opened"


def test_every_level_group_names_itself_and_says_why_it_is_closed():
    """A collapsed group is only an improvement if the summary tells you what is
    inside and why you might open it. `data-title` is the name dev_levels.js
    builds the counted summary from, and it cannot invent one."""

    groups = [group for group in _page().groups if _is_level_group(group)]
    assert groups
    for group in groups:
        attrs = group["attrs"]
        assert attrs.get("data-title"), f"{attrs} has no data-title"
        assert attrs.get("data-hint"), f"{attrs} has no data-hint"
        assert attrs["data-level"] in LEVELS


def test_an_undeclared_level_reads_as_diagnostic():
    """The fallback itself, read out of the shipped rule rather than restated.

    ``dev_levels.js`` is the only place that decides it, and this runs that file
    under node -- the same rule the browser executes, not a copy of it that can
    drift. Skipped where node is absent, like the other node checks in the
    suite.
    """

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    probe = (
        "const api = require(process.argv[1]);"
        "process.stdout.write(JSON.stringify({"
        " common: api.levelOf('common'),"
        " advanced: api.levelOf(' advanced '),"
        " missing: api.levelOf(undefined),"
        " unknown: api.levelOf('bogus'),"
        " blank: api.levelOf(''),"
        " fallback: api.DEFAULT_LEVEL"
        "}));"
    )
    finished = subprocess.run(
        [node, "-e", probe, str(WEB / "dev_levels.js")],
        capture_output=True,
        text=True,
    )
    assert finished.returncode == 0, finished.stderr

    levels = json.loads(finished.stdout)
    assert levels["fallback"] == DEFAULT_LEVEL == "diagnostic"
    assert levels["common"] == "common"
    assert levels["advanced"] == "advanced"
    # The three ways an author fails to declare a level, all landing in the
    # level that is out of the way.
    assert levels["missing"] == DEFAULT_LEVEL
    assert levels["unknown"] == DEFAULT_LEVEL
    assert levels["blank"] == DEFAULT_LEVEL
