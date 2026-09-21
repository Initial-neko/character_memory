"""Archiving a character: UI lifecycle only, never a deletion.

The marker is a file beside ``persona.yaml`` rather than a key inside it,
because the persona document is handed to the model verbatim -- these tests pin
that the definition file comes out of an archive byte-identical.
"""

from pathlib import Path
import re
import shutil
import subprocess

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
        "data-character-archive",
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
    for attribute in re.findall(r"(data-[\w-]+)\s*=", script):
        rendered_here.add(attribute)

    assert read, "the module queries the DOM; the extraction above has gone stale"
    missing = sorted(hook for hook in read - rendered_here if hook not in shell)
    assert missing == [], f"no web source emits these any more: {missing}"
