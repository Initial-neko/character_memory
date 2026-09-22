"""Archiving a character: UI lifecycle only, never a deletion.

The marker is a file beside ``persona.yaml`` rather than a key inside it,
because the persona document is handed to the model verbatim -- these tests pin
that the definition file comes out of an archive byte-identical.
"""

from pathlib import Path
import re
import shutil
import subprocess
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import yaml

from character_memory.api import create_api
from character_memory.avatar_web import attach_avatar_routes
from character_memory.config import (
    ARCHIVE_FILENAME,
    discover_character_profiles,
    load_settings,
    set_character_archived,
)
from character_memory.group_web import attach_group_routes


def _personas(tmp_path: Path) -> Path:
    """A private persona tree. Never the repo's -- archiving writes markers."""

    root = tmp_path / "personas"
    for name, identity in (("rin", "冷静的观察者"), ("momo", "元气的猫娘"), ("rei", "知性的大姐姐")):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "persona.yaml").write_text(
            yaml.safe_dump(
                {"id": name, "name": name.title(), "identity": identity},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    return root


def _config(tmp_path: Path) -> Path:
    personas = _personas(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "archive.db").as_posix()}"',
                f'persona_path: "{(personas / "rin" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_archiving_writes_a_marker_beside_the_persona_and_leaves_it_untouched(tmp_path: Path):
    """The whole point of the sidecar file: the prompt never learns about it."""

    config = _config(tmp_path)
    settings = load_settings(str(config))
    persona_file = Path(settings.persona_path)
    before = persona_file.read_text(encoding="utf-8")

    stamp = set_character_archived(settings, "momo", True)

    marker = persona_file.parent.parent / "momo" / ARCHIVE_FILENAME
    assert marker.is_file()
    assert yaml.safe_load(marker.read_text(encoding="utf-8"))["archived_at"] == stamp
    assert persona_file.read_text(encoding="utf-8") == before

    archived = [item["id"] for item in discover_character_profiles(settings) if "archived_at" in item]
    assert archived == ["momo"]
    momo = next(item for item in discover_character_profiles(settings) if item["id"] == "momo")
    assert momo["archived_at"] == stamp
    # Active characters carry no key at all: key presence *is* the flag.
    assert all("archived_at" not in item for item in discover_character_profiles(settings) if item["id"] != "momo")

    assert set_character_archived(settings, "momo", False) == ""
    assert not marker.exists()
    assert persona_file.read_text(encoding="utf-8") == before
    assert all("archived_at" not in item for item in discover_character_profiles(settings))


def test_unreadable_marker_still_hides_the_character(tmp_path: Path):
    """A hand-broken marker must not silently resurrect a hidden character."""

    config = _config(tmp_path)
    settings = load_settings(str(config))
    marker = Path(settings.persona_path).parent.parent / "rei" / ARCHIVE_FILENAME
    marker.write_text("这不是 YAML 映射： [", encoding="utf-8")

    profile = next(item for item in discover_character_profiles(settings) if item["id"] == "rei")

    assert "archived_at" in profile
    assert profile["archived_at"] == ""


def test_archived_character_leaves_every_picker_but_stays_resolvable(tmp_path: Path):
    config = _config(tmp_path)
    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_avatar_routes(app)

    with TestClient(app) as client:
        listed = {item["id"] for item in client.get("/v1/characters").json()["characters"]}
        assert {"rin", "momo", "rei"} <= listed

        created = client.post("/v1/groups", json={"name": "三人行", "member_ids": ["rin", "momo"]})
        assert created.status_code == 200
        group_id = created.json()["group"]["id"]

        archived = client.post("/v1/characters/momo/archive")
        assert archived.status_code == 200
        assert archived.json()["archived"] is True
        assert archived.json()["archived_at"]

        # Gone from the sidebar, the avatar manager's copy of it, and the group
        # member pickers -- all three read one of these two routes.
        assert "momo" not in {item["id"] for item in client.get("/v1/characters").json()["characters"]}
        assert "momo" not in {item["id"] for item in client.get("/v1/character-profiles").json()["characters"]}
        assert "momo" not in {
            item["id"] for item in client.get("/v1/characters/summaries").json()["characters"]
        }
        assert [item["id"] for item in client.get("/v1/characters?archived=true").json()["characters"]] == [
            "momo"
        ]

        # ... but nothing about the character itself was deleted: history still
        # reads, and the existing group still lists it as a member.
        assert client.get("/v1/chat/history?character_id=momo").status_code == 200
        group = client.get("/v1/groups").json()["groups"][0]
        assert group["id"] == group_id
        assert [item["id"] for item in group["members"]] == ["rin", "momo"]
        assert client.get("/health").json()["runtime_loaded"] is False

        restored = client.post("/v1/characters/momo/restore")
        assert restored.status_code == 200
        assert restored.json()["archived_at"] == ""
        assert "momo" in {item["id"] for item in client.get("/v1/characters").json()["characters"]}
        assert client.get("/v1/characters?archived=true").json()["characters"] == []


def test_archive_api_is_idempotent_and_refuses_unknown_characters(tmp_path: Path):
    config = _config(tmp_path)
    app = create_api(str(config))

    with TestClient(app) as client:
        for _ in range(2):
            assert client.post("/v1/characters/rei/archive").status_code == 200
        assert [item["id"] for item in client.get("/v1/characters?archived=true").json()["characters"]] == ["rei"]

        for _ in range(2):
            assert client.post("/v1/characters/rei/restore").status_code == 200
        assert client.get("/v1/characters?archived=true").json()["characters"] == []

        assert client.post("/v1/characters/nobody/archive").status_code == 404
        assert client.post("/v1/characters/nobody/restore").status_code == 404


def test_archive_refuses_to_hide_the_last_active_character(tmp_path: Path):
    config = _config(tmp_path)
    app = create_api(str(config))

    with TestClient(app) as client:
        assert client.post("/v1/characters/momo/archive").status_code == 200
        assert client.post("/v1/characters/rei/archive").status_code == 200

        response = client.post("/v1/characters/rin/archive")
        assert response.status_code == 409
        assert "至少保留一个未归档人物" in response.text
        assert [item["id"] for item in client.get("/v1/characters").json()["characters"]] == ["rin"]


def test_character_archive_frontend_contract_and_syntax():
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    script_path = web / "character_archive.js"
    script = script_path.read_text(encoding="utf-8")
    index = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "styles.css").read_text(encoding="utf-8")

    assert "/static/character_archive.js" in index
    for token in [
        "character-archive-list-button",
        "archiveCurrentCharacterButton",
        "data-character-archive",
        "data-character-archive-confirm",
        "data-character-restore",
        "?archived=true",
        "/archive",
        "/restore",
    ]:
        assert token in script
    # The row's archive control lives behind the row's "···" menu, so both the
    # trigger and the menu it opens have to be styled.
    for token in ["character-more-button", "character-context-menu", "character-archive-card", "character-archive-list-button"]:
        assert token in css

    node = shutil.which("node")
    if node:
        checked = subprocess.run([node, "--check", str(script_path)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr


def test_every_hook_the_archive_module_reads_is_still_emitted_by_the_shell():
    """The module's selectors are cross-file contracts, and one broke silently.

    It found its sidebar anchor with ``querySelector(".sidebar-title")`` behind
    an ``if`` guard. A later sidebar redesign deleted that element, so no button
    was ever created and the archive drawer became unreachable from the sidebar
    -- no error, no warning, just an entry that was not there. Checking every
    selector this module reads against what the rest of the web sources emit
    turns the next such rename into a failing test instead.

    Selectors the module renders itself are skipped: a name that appears in its
    own markup is not a promise anyone else has to keep, which is why the shared
    key module exists in the first place.
    """

    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    script = (web / "character_archive.js").read_text(encoding="utf-8")
    shell = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(web.glob("*.js")) + sorted(web.glob("*.html"))
        if path.name != "character_archive.js"
    )

    def hook_name(selector: str) -> str:
        return selector.strip("[]").lstrip(".#")

    read = {
        hook_name(value)
        for value in re.findall(r'(?:querySelector|querySelectorAll|closest)\(\s*"([^"]+)"', script)
        # Built at runtime (``CSS.escape``, template literals) -- not a literal
        # anyone can check, and not the kind of hook that broke.
        if "${" not in value
    }
    rendered_here: set[str] = set()
    for attribute in re.findall(r'class="([^"]*)"', script) + re.findall(r'className\s*=\s*"([^"]*)"', script):
        rendered_here.update(attribute.split())
    # Any ``data-`` token outside a selector is an attribute this module writes
    # into its own markup -- bare or with a value; the drawer's cancel button is
    # emitted bare, which is how it slipped past a value-only pattern.
    rendered_here.update(re.findall(r"data-[\w-]+", re.sub(r"\[[^\]]*\]", "", script)))

    assert read, "the module queries the DOM; the extraction above has gone stale"
    missing = sorted(hook for hook in read - rendered_here if hook not in shell)
    assert missing == [], f"no web source emits these any more: {missing}"


def test_archive_frontend_surfaces_a_failed_voice_registry_refresh():
    """Both calls returned the reload result, and both threw it away.

    ``voice_registry`` was in the archive and restore payloads the whole time,
    but the module awaited them as bare statements: a 200 meaning "the sidecar
    never picked this up" looked exactly like a clean archive. The responses are
    held now, and a failed refresh reaches the user instead of the console.

    Telling the user is a message, not a flow change, and the first attempt at
    this conflated the two: it kept the drawer open so the failure had somewhere
    to live. Every archive in a browser without a GSV sidecar then failed to
    reload, so the drawer never closed, its backdrop covered the sidebar, and
    the browser smoke test could no longer click ``.character-archive-entry``
    (``Locator.click: Timeout 10000ms exceeded``). The pins below are the two
    halves that have to stay separate.
    """

    web = Path(__file__).resolve().parents[1] / "src" / "character_memory" / "web"
    script = (web / "character_archive.js").read_text(encoding="utf-8")

    for endpoint in ("/archive`", "/restore${query}`"):
        call = "await CM.api(`/v1/characters/${encodeURIComponent(characterId)}" + endpoint
        assert call in script, endpoint
        assert f"= {call}" in script, f"the {endpoint} response is awaited but discarded"
    assert "voice_registry" in script
    assert "reloaded !== false" in script

    # The message: a notice pinned to the page, which a failure can show without
    # touching the drawer the flow already decided what to do with.
    assert "showVoiceReloadWarning(result);" in script
    assert "archive-voice-notice" in script
    styles = (web / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.archive-voice-notice\s*\{", styles), "the notice is styled where the shell can load it"
    assert re.search(r"\.archive-voice-notice\s*\{[^}]*pointer-events:\s*none", styles), (
        "the notice must not stand between the user and a control underneath it"
    )

    # The flow: archiving closes the drawer on the failing path exactly as it
    # does on a clean one. Presence alone is not enough -- the regressed version
    # still closed the drawer, one early ``return`` further down -- so nothing
    # may leave the function between showing the warning and closing the drawer.
    archive_body = script.split("async function archiveCharacter", 1)[1].split("function renderArchivedDrawer", 1)[0]
    assert "CM.closeDrawer();" in archive_body, "a failed refresh must not hold the drawer open"
    assert "return" not in archive_body.split("showVoiceReloadWarning(result);", 1)[1], (
        "the warning must not be an early exit out of archiving"
    )
    assert "drawerBody" not in archive_body, "the drawer is not the warning's channel any more"


def test_archive_keeps_voice_reference_and_refreshes_live_gsv_registry(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    settings = load_settings(str(config))
    momo_dir = Path(settings.persona_path).parent.parent / "momo"
    (momo_dir / "voice.yaml").write_text("template: murasame\n", encoding="utf-8")
    calls = []

    class FakeReloader:
        def __init__(self, *args, **kwargs):
            calls.append(("init", kwargs.get("timeout_seconds")))

        def reload(self):
            calls.append(("reload", None))

        def close(self):
            calls.append(("close", None))

    monkeypatch.setattr("character_memory.tts_lab.GsvVoiceReloader", FakeReloader)
    app = create_api(str(config))

    with TestClient(app) as client:
        archived = client.post("/v1/characters/momo/archive")
        assert archived.status_code == 200
        assert archived.json()["voice_registry"] == {"ok": True, "reloaded": True}
        assert (momo_dir / "voice.yaml").read_text(encoding="utf-8") == "template: murasame\n"

        restored = client.post("/v1/characters/momo/restore")
        assert restored.status_code == 200
        assert restored.json()["voice_registry"] == {"ok": True, "reloaded": True}
        assert (momo_dir / "voice.yaml").is_file()

    assert [item[0] for item in calls].count("reload") == 2
    assert ("init", 1.5) in calls


def test_archive_says_so_when_it_could_not_refresh_the_voice_registry(tmp_path: Path, monkeypatch):
    """A stale GSV registry is the archive's real failure, and it returned 200.

    ``_rebuild_voices`` keeps the previous ``_archived_character_ids`` whenever
    a single template under the voices tree is unresolvable -- which the GSV
    sidecar treats as an ordinary state, not an incident. Archive still removed
    the row, so the character stayed synthesizable through a sidecar that had
    never heard about it, and the only trace was an ``info`` line. The response
    now carries the failure to the client, and the log says so at a level an
    operator would actually see.

    The module logger is spied on rather than read through ``caplog``: the
    application installs its own handlers and does not propagate, so a record's
    level is only observable where it is emitted.
    """

    config = _config(tmp_path)
    settings = load_settings(str(config))
    momo_dir = Path(settings.persona_path).parent.parent / "momo"
    (momo_dir / "voice.yaml").write_text("template: murasame\n", encoding="utf-8")

    class FailingReloader:
        def __init__(self, *args, **kwargs):
            pass

        def reload(self):
            raise RuntimeError("voices tree has an unresolvable template")

        def close(self):
            pass

    spy = MagicMock()
    monkeypatch.setattr("character_memory.tts_lab.GsvVoiceReloader", FailingReloader)
    monkeypatch.setattr("character_memory.api.logger", spy)
    app = create_api(str(config))

    with TestClient(app) as client:
        archived = client.post("/v1/characters/momo/archive")

    assert archived.status_code == 200
    assert archived.json()["voice_registry"] == {
        "ok": False,
        "reloaded": False,
        "reason": "voices tree has an unresolvable template",
    }
    assert spy.warning.called, "a registry the sidecar did not pick up is not an info-level event"
    assert "voices tree has an unresolvable template" in str(spy.warning.call_args)
