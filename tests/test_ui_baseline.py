"""Guards the shared UI baseline in src/character_memory/web/ui.css.

Three failures this catches, all of which shipped at once before the baseline
existed:

* a page that forgot to load the baseline, or loaded it after its own CSS (so
  the page's private palette won the cascade);
* a class the markup uses that no stylesheet defines -- `.muted` was used a
  dozen times and styled nowhere, and `button.primary` only existed under two
  particular parents, which is why the composer's send button was filled and
  the drawer's save button was not;
* a light literal hardcoded into a page that renders dark -- the Dev Console
  and the TTS Workbench are dark via `data-theme`, and the capture panel used
  to paint #d7dbe3 borders and white cards on top of them.
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"

PAGES = ["index.html", "dev.html", "settings.html", "tts_lab.html"]
BASELINE = "/static/ui.css"

_LINK = re.compile(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"')
_CLASS_ATTR = re.compile(r'class="([^"]*)"')
_CLASS_IN_JS = re.compile(r'classList\.(?:add|remove|toggle)\(([^)]*)\)')
_STRING = re.compile(r'"([^"]+)"')
# A class token, as opposed to the JS values that share those call sites
# (`classList.toggle("active", source === "CAMERA")`) or an interpolated
# fragment (`"intent-${item.status}"`, which no stylesheet can name).
_TOKEN = re.compile(r"^[a-z][a-z0-9-]*$")

# Query hooks that ride a class which *is* styled: app.js looks for
# `.typing-row` on an element that is already a `.message-row`, and everything
# it wraps is styled by name. Nothing for a stylesheet to do.
HOOKS = {"typing-row"}


def _stylesheets(page: str) -> list[str]:
    return _LINK.findall((WEB / page).read_text(encoding="utf-8"))


def test_every_page_loads_the_shared_baseline_first():
    for page in PAGES:
        hrefs = _stylesheets(page)
        assert BASELINE in hrefs, page
        assert hrefs[0] == BASELINE, f"{page} must load the baseline before its own CSS"


def test_every_class_the_markup_uses_is_styled_somewhere():
    css = "\n".join(path.read_text(encoding="utf-8") for path in WEB.glob("*.css"))
    defined = set(re.findall(r"\.(-?[A-Za-z_][\w-]*)", css))

    used: set[str] = set()
    for path in list(WEB.glob("*.html")) + list(WEB.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for attr in _CLASS_ATTR.findall(text):
            used.update(attr.split())
        for call in _CLASS_IN_JS.findall(text):
            for literal in _STRING.findall(call):
                used.update(literal.split())

    assert {name for name in used if _TOKEN.match(name)} - defined - HOOKS == set()


_AT_RULE = re.compile(r"@(?:media|supports|keyframes|layer)[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", re.S)
_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _uncommented(css: str) -> str:
    """Comments out of the way: a comma inside one would split a selector."""
    return _COMMENT.sub("", css)


def _targets(selectors: str, selector: str) -> bool:
    """True when one comma-separated part of `selectors` is `selector`.

    Exact, not a substring: `.character-item` also appears inside
    `.character-item-wrap`, and matching that would hand back the wrong rule.
    A trailing pseudo-class is allowed so `:hover`/`:focus-visible` variants of
    a base rule are still recognisable.
    """
    return any(part.strip() == selector or part.strip().startswith(f"{selector}:")
               for part in selectors.split(","))


def _rule(css: str, selector: str) -> str:
    """The body of the base rule for `selector`, ignoring responsive overrides.

    Media blocks are stripped first so a mobile restatement of the same
    selector cannot shadow the rule an assertion is about.
    """
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", _AT_RULE.sub("", _uncommented(css)))
    matching = [body for selectors, body in rules if _targets(selectors, selector)]
    assert matching, f"no rule for {selector}"
    return matching[0]


def _declaration(body: str, prop: str) -> str | None:
    match = re.search(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", body)
    return match.group(1).strip() if match else None


def test_topbar_shares_its_row_with_the_character_heading():
    """The topbar must never win its width fight against the avatar.

    Every rule pinned here is one whose absence produced the same visible bug:
    the nowrap tagline kept its min-content width and painted underneath the
    buttons, and the avatar -- a plain flex item -- was the first thing to
    collapse to zero.
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'class="character-heading-copy"' in html
    assert "/static/topbar_menu.js" in html

    css = (WEB / "styles.css").read_text(encoding="utf-8")
    copy = _rule(css, ".character-heading-copy")
    assert _declaration(copy, "min-width") == "0", "the tagline cannot shrink without this"
    assert _declaration(_rule(css, ".header-avatar"), "flex") == "0 0 38px", "the avatar must not collapse"
    assert _declaration(_rule(css, ".topbar-actions"), "flex") == "0 0 auto"


def test_character_row_reserves_a_lane_wide_enough_for_its_archive_button():
    """A shorthand that overwrites the button's lane puts the unread dot under it.

    `.character-item` used to set `padding-right:38px` in one rule and
    `padding:10px` in a later one, so the lane was 10px and the two collided.
    """
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    padding = _declaration(_rule(css, ".character-item"), "padding").split()
    assert len(padding) == 4, "the lane has to be part of the shorthand"
    top, right, bottom, left = (int(value.removesuffix("px")) for value in padding)
    assert top == bottom
    assert right >= 56, "the lane the archive button sits in"
    assert right > left

    mobile = _AT_RULE.findall(css)
    assert any("58px" in block for block in mobile), "the mobile rule must keep the lane"


def test_archiving_a_character_is_a_labelled_button_not_a_hover_trigger():
    """It has no other entry point, so it cannot be hidden or unnamed.

    The row used to carry a "···" at 45% opacity that opened a menu holding
    this one action. It was invisible enough that the action could not be
    found, so the trigger is now the action.
    """
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'class="character-archive-button"' in app
    assert 'data-character-more' not in app

    button = _rule((WEB / "styles.css").read_text(encoding="utf-8"), ".character-archive-button")
    assert _declaration(button, "opacity") in (None, "1")
    assert _declaration(button, "border"), "an unstyled button does not read as one"

    # The group row keeps its "···" menu, but still must not be hover-only.
    groups = _rule((WEB / "p0_11.css").read_text(encoding="utf-8"), ".group-more-button")
    assert _declaration(groups, "opacity") not in (None, "0")


def _luminance(value: str) -> float:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    red, green, blue = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def test_dark_pages_do_not_paint_light_surfaces():
    """A page that renders dark must not hardcode a light background.

    The baseline's dark block is the only place a dark page decides what white
    is; every other background must be a token so it follows the theme.
    """
    backgrounds = re.compile(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,6})\b")
    for page in ["dev.html", "tts_lab.html"]:
        assert re.search(r'data-theme="dark"', (WEB / page).read_text(encoding="utf-8")), page
        for href in _stylesheets(page):
            sheet = WEB / href.rsplit("/", 1)[-1]
            for literal in backgrounds.findall(sheet.read_text(encoding="utf-8")):
                assert _luminance(literal) < 0.85, f"{sheet.name}: {literal} on a dark page"
