"""Wiring checks for the character voice panel.

voices.js is a plain browser script with no module system, so there is nothing
to import it into. These assertions pin the things that break silently: the
module is registered, the entry point is wired, and the panel reads the fields
the API actually returns.

The one thing a source match *can* prove here is agreement between two files
that never see each other -- the panel and the route it calls, the panel and its
stylesheet. Those are asserted as pairs, because a rename on one side alone is
invisible until someone opens the drawer.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_the_panel_is_registered_as_a_feature():
    source = (WEB / "voices.js").read_text(encoding="utf-8")

    assert 'CM.registerFeature("voice"' in source
    assert "function open(characterId = CM.state.characterId)" in source


def test_the_panel_is_loaded_by_the_app_shell():
    html = (WEB / "index.html").read_text(encoding="utf-8")

    assert "/static/voices.js" in html
    assert 'href="/static/voices.css"' in html


def test_the_character_name_opens_the_panel():
    """The avatar panel is opened from the header avatar; this is its sibling."""

    source = (WEB / "voices.js").read_text(encoding="utf-8")

    assert "characterName" in source
    assert "addEventListener" in source


def test_the_entry_point_element_exists_in_the_shell():
    """``CM.dom.characterName`` is null without this id, and the panel is unreachable."""

    html = (WEB / "index.html").read_text(encoding="utf-8")

    assert 'id="characterName"' in html


def test_the_panel_reads_the_fields_the_endpoint_returns():
    """The names must match voice_snapshot()'s output, not a guess at it."""

    source = (WEB / "voices.js").read_text(encoding="utf-8")

    assert "/v1/voice-templates" in source
    for field in ("templates", "characters", "status", "error"):
        assert field in source, f"the panel must handle {field}"


def test_the_panel_posts_to_the_path_the_server_registers():
    """A rename on either side is silent: the drawer just stops saving."""

    source = (WEB / "voices.js").read_text(encoding="utf-8")
    server = (ROOT / "src" / "character_memory" / "voice_web.py").read_text(encoding="utf-8")

    assert '"/v1/characters/{character_id}/voice"' in server
    assert "`/v1/characters/${encodeURIComponent(characterId)}/voice`" in source


def test_the_panel_shows_who_else_uses_a_template():
    """Spec 11: without this, a shared overwrite changes someone else's voice
    with no warning anywhere in the UI."""

    source = (WEB / "voices.js").read_text(encoding="utf-8")

    assert "used_by" in source


def test_the_panel_classes_are_styled_by_its_own_stylesheet():
    """The markup and the stylesheet are two files that never meet at runtime."""

    source = (WEB / "voices.js").read_text(encoding="utf-8")
    css = (WEB / "voices.css").read_text(encoding="utf-8")

    assert "voice-panel" in source
    assert ".voice-panel" in css
    assert ".voice-editable" in css


def test_the_panel_does_not_share_a_stylesheet_with_the_call_ui():
    """voice.css is the microphone/playback UI; a voice line is a different thing."""

    assert (WEB / "voices.css").is_file()
    assert not (WEB / "voice.css").read_text(encoding="utf-8").count("voice-panel")


# --- the TTS Lab's half of the freeze path -----------------------------------
#
# The Workbench talks to the Lab on :9002 rather than to the chat app, so its
# contract is checked against tts_lab.py's own route table.


def test_the_lab_offers_saving_a_template_without_a_character():
    source = (WEB / "tts_lab.js").read_text(encoding="utf-8")

    assert "save-template" in source
    assert "voiceDesignTemplateName" in source


def test_the_lab_warns_before_overwriting_a_shared_template():
    """Overwriting changes other characters' voices; the user must see that first."""

    source = (WEB / "tts_lab.js").read_text(encoding="utf-8")

    assert "shared_with" in source


def test_the_lab_markup_carries_the_save_controls_the_script_reads():
    """``$()`` returns null for a missing id, and the panel dies at load."""

    html = (WEB / "tts_lab.html").read_text(encoding="utf-8")

    assert 'id="saveVoiceDesignTemplate"' in html
    assert 'id="voiceDesignTemplateName"' in html
    assert 'id="voiceDesignTemplateStatus"' in html


def test_the_lab_save_control_is_wired():
    source = (WEB / "tts_lab.js").read_text(encoding="utf-8")

    assert '$("saveVoiceDesignTemplate").addEventListener("click", saveVoiceDesignTemplate)' in source


def test_the_lab_posts_to_the_path_the_lab_registers():
    """Both halves of the cross-process contract, spelled the same."""

    source = (WEB / "tts_lab.js").read_text(encoding="utf-8")
    lab = (ROOT / "src" / "character_memory" / "tts_lab.py").read_text(encoding="utf-8")

    assert '"/v1/voice-design/save-template"' in lab
    assert '"/v1/voice-design/save-template"' in source


def test_the_panel_leaves_the_group_rename_tooltip_alone():
    """group_settings.js renames the group from this same header element.

    Setting the title unconditionally would clobber "点击修改群名称" the first
    time a group is opened, and clearing it would erase that tooltip instead.
    """

    source = (WEB / "voices.js").read_text(encoding="utf-8")

    assert 'if (!CM.isGroupConversation()) CM.dom.characterName.title = "设置声线";' in source
    assert "removeAttribute" not in source
