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
