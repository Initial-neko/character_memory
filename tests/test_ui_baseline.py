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


def _web_rules(path):
    """Every selector/body pair in a sheet, media-nested rules included."""

    return re.findall(r"([^{}]+)\{([^{}]*)\}", _uncommented(path.read_text(encoding="utf-8")))


# The row's "···" trigger is absolutely positioned inside the row's right
# padding, so that lane has to hold it. Three sheets restate the row -- the base
# one for the mobile block, chat_refine.css for every width -- and any of them
# can win, so all of them are checked against the widest placement.
TRIGGER_LANE = 34  # right:7px plus a 27px trigger


def test_no_sheet_gives_the_character_row_a_lane_narrower_than_its_trigger():
    """A shorthand that drops the lane puts the copy and the unread dot under it.

    `.character-item` used to set `padding-right:38px` in one rule and
    `padding:10px` in a later one, so the lane was 10px and the two collided.
    Every restatement is a chance to lose it again, so they are all checked.
    """
    offenders = []
    for path in sorted(WEB.glob("*.css")):
        for selectors, body in _web_rules(path):
            # Only the row's own rule: a layout-mode variant (the collapsed
            # rail) hides the trigger outright and reserves no lane for it.
            if not _targets(selectors.strip(), ".character-item"):
                continue
            padding = _declaration(body, "padding")
            if not padding:
                continue
            parts = padding.split()
            if len(parts) != 4:
                offenders.append((path.name, padding))
                continue
            if int(parts[1].removesuffix("px")) < TRIGGER_LANE:
                offenders.append((path.name, padding))
    assert offenders == [], f"the lane has to be in the shorthand, and wide enough: {offenders}"


def test_no_stylesheet_hides_the_character_row_trigger_at_rest():
    """It is the only way to archive, so no sheet may take it off the screen.

    styles.css dims the trigger at rest for exactly this reason -- on a touch
    screen a control that only exists on hover does not exist -- and
    chat_refine.css, which loads last, restated it as ``opacity: 0`` and undid
    that without a word. A sheet may still hide it in a state the user can see
    or leave (hover, focus, the collapsed rail); it may not hide it outright.
    """
    stateful = (":hover", ":focus", "collapsed")
    offenders = []
    for path in sorted(WEB.glob("*.css")):
        for selectors, body in _web_rules(path):
            if ".character-more-button" not in selectors:
                continue
            if any(marker in selectors for marker in stateful):
                continue
            if _declaration(body, "opacity") == "0" or _declaration(body, "display") == "none":
                offenders.append(path.name)
    assert offenders == [], f"these sheets hide the trigger at rest: {offenders}"


def test_archiving_a_character_stays_findable_behind_its_row_trigger():
    """The trigger has to be reachable by keyboard, not only by pointer.

    The archive action lives in the row's "···" menu, and a menu that cannot be
    opened without a pointer is not reachable at all for keyboard users -- the
    same failure as hover-only, one input device over.
    """
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'class="character-more-button"' in app
    assert 'data-character-more' in app
    assert 'class="character-context-menu hidden"' in app
    assert 'data-character-archive' in app, "the menu has to hold the action"

    css = (WEB / "styles.css").read_text(encoding="utf-8")
    keyboard = [
        body
        for selectors, body in _web_rules(WEB / "styles.css")
        if ":focus-visible" in selectors and _targets(selectors, ".character-more-button")
    ]
    assert keyboard, "the trigger has to be reachable without a pointer"
    assert _declaration(keyboard[0], "opacity") == "1"

    # The group row keeps its "···" menu, but still must not be hover-only.
    groups = _rule((WEB / "p0_11.css").read_text(encoding="utf-8"), ".group-more-button")
    assert _declaration(groups, "opacity") not in (None, "0")


_TOKEN_DEF = re.compile(r"--([a-z0-9-]+)\s*:\s*([^;]+);")


def _tokens(css: str, block: str = "") -> dict[str, str]:
    """Declared token values, from the whole sheet or from one opening block.

    First declaration wins: the sheet restates the same names in more than one
    block (the light `:root`, the dark override), and the value a reader gets
    is the one the cascade resolves, not the last line in the file.
    """

    text = _uncommented(css)
    if block:
        start = text.index(block)
        text = text[start : text.index("}", start)]
    tokens: dict[str, str] = {}
    for name, value in _TOKEN_DEF.findall(text):
        tokens.setdefault(name, value.strip())
    return tokens


def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _relative_luminance(colour: str) -> float:
    value = colour.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    red, green, blue = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def _contrast(foreground: str, background: str) -> float:
    high, low = sorted((_relative_luminance(foreground), _relative_luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_muted_text_clears_wcag_aa_on_every_light_surface():
    """The smallest type in the app is the type that has to clear 4.5:1.

    ``--ui-muted`` paints timestamps, image captions and help text -- 10px and
    11px -- and it was #7b7f87: 4.02:1 on white, 3.56:1 on the sunken surface
    the image caption actually sits on. Both are under WCAG AA. Dark is fine
    (#8b949e on #161b22 is 5.62:1) and is checked so a light-only fix cannot
    quietly take it down with it.
    """

    css = (WEB / "ui.css").read_text(encoding="utf-8")
    light = _tokens(css)
    dark = _tokens(css, ':root[data-theme="dark"]')
    assert light["ui-muted"] != dark["ui-muted"], "dark must keep its own muted"

    for surface in ("ui-surface", "ui-surface-soft", "ui-bg", "ui-surface-sunken"):
        ratio = _contrast(light["ui-muted"], light[surface])
        assert ratio >= 4.5, f"--ui-muted on --{surface}: {ratio:.2f}:1"

    for surface in ("ui-surface", "ui-surface-soft", "ui-bg"):
        ratio = _contrast(dark["ui-muted"], dark[surface])
        assert ratio >= 4.5, f"dark --ui-muted on --{surface}: {ratio:.2f}:1"


def test_faint_text_clears_wcag_aa_on_every_light_surface():
    """``--ui-faint`` is the other half of the small type, and it failed too.

    It paints the message timestamps (10px) and the date separators (11px) --
    2.53:1 on white and 2.24:1 on the sunken surface as #a0a3aa, so half the
    transcript's metadata was unreadable at the size it is set in. It also
    paints the sticker panel's footer and the composer hint, and on the sunken
    surface the broken-sticker fallback, so the value has to clear 4.5:1 on
    every surface a page can put it on, not just on white.

    The two tokens are now close together (5.19:1 against 5.27:1 on white):
    that is the whole of the room WCAG AA leaves for a de-emphasised grey at
    10px, and a lighter `faint` is the failing token this test exists to stop.
    """

    css = (WEB / "ui.css").read_text(encoding="utf-8")
    light = _tokens(css)
    dark = _tokens(css, ':root[data-theme="dark"]')
    assert light["ui-faint"] != dark["ui-faint"], "dark must keep its own faint"

    # De-emphasis survives the fix: `faint` still reads lighter than `muted`.
    assert _relative_luminance(light["ui-faint"]) > _relative_luminance(light["ui-muted"])

    for surface in ("ui-surface", "ui-surface-soft", "ui-bg", "ui-surface-sunken"):
        ratio = _contrast(light["ui-faint"], light[surface])
        assert ratio >= 4.5, f"--ui-faint on --{surface}: {ratio:.2f}:1"

    # No dark page paints with `faint` today (the Dev Console and the TTS
    # Workbench only use `muted`), which is exactly why the value has to be
    # checked: the first page that reaches for it must not inherit a failing
    # #6e7681 (3.77:1 on --ui-surface, 3.31:1 on --ui-surface-soft).
    for surface in ("ui-surface", "ui-surface-soft", "ui-bg"):
        ratio = _contrast(dark["ui-faint"], dark[surface])
        assert ratio >= 4.5, f"dark --ui-faint on --{surface}: {ratio:.2f}:1"


def test_a_sticker_is_a_message_sized_box_not_a_shrink_to_fit_image():
    """A sticker used to render at whatever its containing block allowed.

    The bundled stickers are SVGs with a viewBox and no width/height, so they
    have no intrinsic width: the lazy ``img`` was laid out 0x0 first, the
    shrink-to-fit ``.bubble-wrap`` settled on its timestamp, and the loaded
    sticker inherited 71px -- next to a 360px image bubble. An explicit box is
    what keeps the message rhythm, so it is pinned here.
    """

    css = (WEB / "p0_7.css").read_text(encoding="utf-8")
    img = _rule(css, ".sticker-bubble img")
    size = "--sticker-size"
    assert _declaration(img, "width") == f"var({size})"
    assert _declaration(img, "height") == f"var({size})"
    assert _declaration(img, "max-width") == f"var({size})"
    assert _declaration(img, "max-height") == f"var({size})"
    assert _declaration(img, "object-fit") == "contain", "an explicit box must not squash a wide sticker"
    assert _declaration(_rule(css, ".sticker-bubble"), size) == "112px"

    # Both surfaces take the one size, and both narrow together on mobile.
    group = _rule((WEB / "p0_11.css").read_text(encoding="utf-8"), ".group-message-sticker")
    assert _declaration(group, "width") is None, "the group must not size stickers on its own"

    for sheet in ("p0_7.css", "p0_11.css"):
        responsive = _AT_RULE.findall((WEB / sheet).read_text(encoding="utf-8"))
        assert any(f"{size}: 96px" in block for block in responsive), sheet


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
