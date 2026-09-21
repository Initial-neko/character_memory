from pathlib import Path
import shutil
import subprocess


def test_sidebar_shell_has_stable_slots_and_no_dev_footer():
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    index = (web / "index.html").read_text(encoding="utf-8")

    for token in [
        'id="sidebarSpace"',
        'id="sidebarCharacters"',
        'id="characterActions"',
        'id="sidebarGroups"',
        'id="sidebarCollapseButton"',
        'id="sidebarMobileButton"',
        'id="sidebarCharacterMore"',
    ]:
        assert token in index

    assert "Phase 1 · Prove the Person" not in index


def test_sidebar_modules_target_shell_and_have_valid_javascript():
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"

    expected = {
        "sidebar_collapse.js": [
            "MAX_RAIL_CHARACTERS = 6",
            "character-memory:recent-characters",
            "rail-visible",
            "sidebar-mobile-open",
        ],
        "space.js": ['getElementById("sidebarSpace")', "characterSpaceButton"],
        "groups.js": ['getElementById("sidebarGroups")', "<span>群聊</span>"],
        "character_archive.js": [
            'getElementById("characterActions")',
            "archiveCurrentCharacterButton",
            "data-character-archive-confirm",
        ],
        "topbar_menu.js": ["characterSpaceButton", "archiveCurrentCharacterButton"],
    }

    node = shutil.which("node")
    for filename, tokens in expected.items():
        path = web / filename
        source = path.read_text(encoding="utf-8")
        for token in tokens:
            assert token in source
        if node:
            checked = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            assert checked.returncode == 0, checked.stderr


def test_sidebar_css_defines_expanded_compact_and_mobile_modes():
    root = Path(__file__).resolve().parents[1]
    css = (root / "src" / "character_memory" / "web" / "chat_refine.css").read_text(encoding="utf-8")

    for token in [
        "--cm-sidebar-expanded: 280px",
        "--cm-sidebar-compact: 68px",
        '.sidebar[data-mode="compact"]',
        ".character-item-wrap:not(.rail-visible)",
        "@media (max-width: 820px)",
        "scrollbar-width: none",
    ]:
        assert token in css
