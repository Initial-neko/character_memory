"""Raw JSON stays behind a click.

Reported behaviour: a page (or a drawer) handed the user a wall of raw JSON they
never asked for, the moment it loaded. The rule pinned here is structural: a
debug payload lives inside a collapsed ``<details class="debug-output">``, and
the outcome a person actually reads -- badge, latency, transcript, model reply --
stays outside it.

The markup is parsed into a tree and each ``<pre>`` is judged against its real
ancestors, so a block that escapes its ``<details>``, or a ``<details>`` that
gains ``open``, fails here however the source is reflowed. The chat drawer is
built from a JS template literal, so that one is read out of ``app.js`` with a
small template scanner and parsed the same way.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"

PAGES = ["index.html", "dev.html", "settings.html", "tts_lab.html"]
BASELINE = "/static/ui.css"
_LINK = re.compile(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"')

# The blocks that are meant to be read at rest. Each holds what the run
# produced as text -- a model reply, a transcript, the polished prompt -- and
# never a JSON.stringify dump, so a click in front of it would only hide the
# answer. Everything else in a page belongs behind the collapsed debug block.
HUMAN_OUTPUT = {
    "index.html": set(),
    "settings.html": set(),
    "dev.html": {"llmReply", "visionReply", "imageRewriteResult"},
    "tts_lab.html": {"voiceDesignFreezeStatus", "voiceDesignTemplateStatus"},
}

# Sections of the chat drawer that dump a structure or a raw response body.
# The rest of the drawer renders strings (perception, context, persona), which
# is why they are not here.
DRAWER_RAW_SECTIONS = ["Sticker Retrieval", "Memory Admission", "Memory Write", "Intent", "Raw Model Response"]


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Markup(HTMLParser):
    """A document, remembered only as far as these assertions need it.

    Per ``<pre>``: its attributes, the nearest ``<details>`` ancestor, and the
    last ``<h3>`` that closed before it (the drawer labels its blocks that way).
    Per ``<details>``: its attributes and whether a ``<summary>`` sits directly
    inside it -- without one there is nothing to click.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict]] = []
        self.pres: list[tuple[dict, dict | None, str]] = []
        self.details: list[tuple[dict, bool]] = []
        self._heading: list[str] | None = None
        self._last_heading = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs or {})
        if tag == "pre":
            self.pres.append((attrs, self._nearest("details"), self._last_heading))
        elif tag == "details":
            self.details.append((attrs, False))
        elif tag == "summary" and self.stack and self.stack[-1][0] == "details":
            attributes, _ = self.details[-1]
            self.details[-1] = (attributes, True)
        elif tag == "h3":
            self._heading = []
        if tag not in _VOID:
            self.stack.append((tag, attrs))

    def handle_data(self, data):
        if self._heading is not None:
            self._heading.append(data)

    def handle_endtag(self, tag):
        if tag == "h3" and self._heading is not None:
            self._last_heading = "".join(self._heading).strip()
            self._heading = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def _nearest(self, name: str) -> dict | None:
        for tag, attrs in reversed(self.stack):
            if tag == name:
                return attrs
        return None


def _parse(markup: str) -> _Markup:
    parsed = _Markup()
    parsed.feed(markup)
    parsed.close()
    return parsed


def _read_template(source: str, start: int) -> tuple[str, int]:
    """The JS template literal starting at the backtick ``start``, holes blanked.

    ``${...}`` expressions and nested template literals are replaced by the
    markup they build (usually nothing) so the result is the element skeleton
    the browser will end up with -- parseable, and still carrying the nesting
    these assertions are about.
    """
    assert source[start] == "`", source[max(0, start - 40) : start + 40]
    out: list[str] = []
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            out.append(source[index : index + 2])
            index += 2
        elif char == "`":
            return "".join(out), index + 1
        elif char == "$" and source[index + 1 : index + 2] == "{":
            index, built = _skip_hole(source, index)
            out.append(built)
        else:
            out.append(char)
            index += 1
    raise AssertionError("unterminated JS template literal")


def _skip_hole(source: str, start: int) -> tuple[int, str]:
    """Index just past the ``}`` closing the ``${`` at ``start``, plus any markup
    a template literal nested inside the hole builds."""
    depth = 0
    built: list[str] = []
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if not depth:
                return index + 1, "".join(built)
        elif char == "`":
            markup, index = _read_template(source, index)
            built.append(markup)
            continue
        index += 1
    raise AssertionError("unterminated ${...} hole")


def _drawer_markup(source: str, function_header: str) -> str:
    """The drawer HTML one ``CM.show*`` function builds as a template literal.

    Only the template-literal assignment is read: the same function also writes
    a plain-string placeholder, which carries no markup to check.
    """
    body = source[source.index(function_header) :]
    marker = "drawerBody.innerHTML = `"
    start = body.index(marker) + len(marker) - 1  # the opening backtick
    markup, _ = _read_template(body, start)
    return markup


def _uncovered(parsed: _Markup, allowed_ids: set[str], allowed_headings: set[str]) -> list[str]:
    """Raw blocks that are on screen without a click."""
    exposed = []
    for attrs, details, heading in parsed.pres:
        if attrs.get("id") in allowed_ids or heading in allowed_headings:
            continue
        if details is None or "open" in details:
            exposed.append(attrs.get("id") or heading or "<pre>")
    return exposed


@pytest.mark.parametrize("page", PAGES)
def test_no_page_renders_a_raw_block_at_rest(page):
    parsed = _parse((WEB / page).read_text(encoding="utf-8"))
    assert _uncovered(parsed, HUMAN_OUTPUT[page], set()) == []


def _assert_blocks_start_collapsed_and_openable(parsed: _Markup, where: str) -> None:
    """A block that arrives expanded, or without a summary, hides the payload
    exactly as badly as no block at all -- one floods the page, the other cannot
    be opened."""
    for attrs, has_summary in parsed.details:
        label = attrs.get("class") or attrs.get("id") or "<details>"
        assert "open" not in attrs, f"{where}: {label} starts expanded"
        assert has_summary, f"{where}: {label} has no <summary>, so it cannot be opened"


@pytest.mark.parametrize("page", PAGES)
def test_every_debug_block_is_collapsed_and_has_something_to_click(page):
    _assert_blocks_start_collapsed_and_openable(_parse((WEB / page).read_text(encoding="utf-8")), page)


def _defines(sheet: str, selector: str) -> bool:
    """True when `sheet` carries a rule whose selector is exactly `selector`.

    Trailing variants (`.debug-output > summary`) are not the rule being looked
    for, so the selector has to end the group or open the block on its own.
    """
    return re.search(rf"(?:^|[,\s]){re.escape(selector)}\s*[,{{]", sheet) is not None


def test_the_collapsed_style_lives_in_a_sheet_every_page_loads():
    """dev.css was the only home for these rules, and index.html -- the page that
    loads app.js and builds the drawer -- never loads it. Kept in one sheet the
    whole app already loads, so a page cannot use the class and get no styling.
    """
    owners = sorted(path.name for path in WEB.glob("*.css") if _defines(path.read_text(encoding="utf-8"), ".debug-output"))
    assert owners == ["ui.css"], f"exactly one home for the shared rule, found {owners}"
    for page in PAGES:
        assert BASELINE in _LINK.findall((WEB / page).read_text(encoding="utf-8")), page
    assert _defines((WEB / "ui.css").read_text(encoding="utf-8"), ".debug-output > summary")


def test_chat_drawer_keeps_its_raw_dumps_collapsed():
    source = (WEB / "app.js").read_text(encoding="utf-8")
    templates = [_drawer_markup(source, header) for header in ("CM.showTrace = async", "CM.showRuntime = async")]

    human_sections = {
        "耗时",
        "决策",
        "最终对外表达",
        "Mental State",
        "Mental State · Before",
        "Mental State · After",
        "Recall",
        "实际发送给模型的 messages",
        "Compiled Context",
        "Persona",
        "Provider",
        "Runtime 初始化耗时",
        "Memory Inspector",
    }
    wrapped: set[str] = set()
    for markup in templates:
        parsed = _parse(markup)
        _assert_blocks_start_collapsed_and_openable(parsed, "the chat drawer")
        assert _uncovered(parsed, set(), human_sections) == []
        wrapped |= {
            heading
            for _, details, heading in parsed.pres
            if details is not None and "open" not in details and "debug-output" in (details.get("class") or "")
        }

    # The sections that *are* dumps still have to exist, so deleting one cannot
    # make the assertion above pass by vacuity.
    for section in DRAWER_RAW_SECTIONS:
        assert any(heading.startswith(section) for heading in wrapped), f"{section} is no longer a collapsed block"


# Two halves of one wiring contract: the id has to exist in the page, and the
# script that fills it has to name it the same way. Renaming one side only is a
# silent failure -- the block simply stops updating.
WIRING = {
    "dev.js": {
        "characterStatus",
        "mediaStatus",
        "modelStatus",
        "llmReply",
        "llmResult",
        "ttsLatency",
        "ttsResult",
        "asrText",
        "asrResult",
        "mediaSmokeText",
        "mediaSmokeResult",
        "resourceSummary",
        "spaceRunId",
        "loadSpaceRunRaw",
        "spaceRunResult",
    },
    "dev_capture.js": {"visionReply", "visionResult", "visionLatency"},
    "dev_visual.js": {"imageProviderStatus", "imageGenResult", "imageGenLatency"},
    "tts_lab.js": {"ttsLabSummary", "ttsLabResult", "voiceDesignStatus", "voiceDesignResult", "voiceDesignResultSummary"},
}


def test_a_collapsed_block_is_still_fed_by_the_script_that_owns_it():
    pages = {name: (WEB / name).read_text(encoding="utf-8") for name in ("dev.html", "tts_lab.html")}
    for script_name, ids in WIRING.items():
        script = (WEB / script_name).read_text(encoding="utf-8")
        for element_id in sorted(ids):
            assert f'id="{element_id}"' in pages["dev.html"] or f'id="{element_id}"' in pages["tts_lab.html"], element_id
            assert f'$("{element_id}")' in script, f"{script_name} no longer writes {element_id}"
