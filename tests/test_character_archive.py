"""Archiving a character: UI lifecycle only, never a deletion.

The marker is a file beside ``persona.yaml`` rather than a key inside it,
because the persona document is handed to the model verbatim -- these tests pin
that the definition file comes out of an archive byte-identical.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
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


@contextmanager
def _fake_sidecar(*, status: int = 200, detail: str | None = None, delay: float = 0.0):
    """The reload route, on a real port, answered by a real socket.

    These tests used to patch ``GsvVoiceReloader`` out and assert on the dict the
    app built from what the patch raised. That pinned the app's own arithmetic
    and nothing else: the distinction the route now has to make -- a dead port
    versus an answered refusal -- is decided below that seam, by httpx, on the
    wire. So the reloader is left alone and its peer is faked instead.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 -- the stdlib's spelling, not ours
            if delay:
                time.sleep(delay)
            body = json.dumps({"detail": detail, "loaded": status < 400, "voices": []}).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # the client gave up and closed the connection first

        def log_message(self, *args):  # a fake sidecar, not a chatty one
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _dead_sidecar_url() -> str:
    """A port that was bound and released, so nothing is listening on it."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return f"http://127.0.0.1:{port}"


def _voice_registry_after_archive(tmp_path: Path, monkeypatch, base_url: str, log=None) -> dict:
    """Archive a character for real, against ``base_url``, and report what the
    route said about the registry.

    The archive itself has to succeed on every path -- that is the whole reason
    the reload result is a field and not an error code -- so it is asserted here
    once rather than repeated per outcome.
    """

    config = _config(tmp_path)
    settings = load_settings(str(config))
    momo_dir = Path(settings.persona_path).parent.parent / "momo"
    (momo_dir / "voice.yaml").write_text("template: murasame\n", encoding="utf-8")
    monkeypatch.setenv("GSV_TTS_BASE_URL", base_url)
    if log is not None:
        monkeypatch.setattr("character_memory.api.logger", log)

    with TestClient(create_api(str(config))) as client:
        archived = client.post("/v1/characters/momo/archive")

    assert archived.status_code == 200, archived.text
    assert (momo_dir / "voice.yaml").read_text(encoding="utf-8") == "template: murasame\n"
    return archived.json()["voice_registry"]


def test_archive_reports_a_sidecar_that_took_the_new_roster(tmp_path: Path, monkeypatch):
    with _fake_sidecar(status=200) as url:
        registry = _voice_registry_after_archive(tmp_path, monkeypatch, url)

    assert registry == {"ok": True, "reloaded": True, "status": "reloaded"}


def test_archive_reports_a_sidecar_that_refused_the_reload(tmp_path: Path, monkeypatch):
    """A stale GSV registry is the archive's real failure, and it returned 200.

    ``_rebuild_voices`` keeps the previous ``_archived_character_ids`` whenever
    a single template under the voices tree is unresolvable -- which the GSV
    sidecar treats as an ordinary state, not an incident. Archive still removed
    the row, so the character stayed synthesizable through a sidecar that had
    never heard about it, and the only trace was an ``info`` line. The response
    now carries the failure to the client, and the log says so at a level an
    operator would actually see. The sidecar's own sentence comes through with
    it: it names the file that would not parse, which is the one fact this
    process cannot reconstruct from a status code.

    The module logger is spied on rather than read through ``caplog``: the
    application installs its own handlers and does not propagate, so a record's
    level is only observable where it is emitted.
    """

    spy = MagicMock()
    with _fake_sidecar(status=400, detail="voices/momo.yaml: template 'murasame' is not registered") as url:
        registry = _voice_registry_after_archive(tmp_path, monkeypatch, url, log=spy)

    assert registry["ok"] is False
    assert registry["reloaded"] is False
    assert registry.get("status") == "rejected", registry
    assert "HTTP 400" in registry["reason"]
    assert "murasame" in registry["reason"]
    assert spy.warning.called, "a registry the sidecar did not pick up is not an info-level event"
    assert "murasame" in str(spy.warning.call_args)
    assert "unreachable" not in str(spy.info.call_args_list), "a refusal answered; nothing was unreachable"


def test_archive_does_not_invent_a_sidecar_that_is_not_running(tmp_path: Path, monkeypatch):
    """The common deployment, and the one that was being lied about.

    ``dev_stack`` only starts a GSV sidecar when ``.external/GSV-TTS-Lite/.venv``
    exists, and the usual ``tts_provider`` is not ``gsv``. This arrives at the
    browser as the same ``reloaded: false`` a real refusal gets, so it hung a
    permanent "a running GSV sidecar may still be synthesizing the old roster"
    banner over a machine that had no sidecar to synthesize with.
    """

    spy = MagicMock()
    registry = _voice_registry_after_archive(tmp_path, monkeypatch, _dead_sidecar_url(), log=spy)

    assert registry["ok"] is False
    assert registry["reloaded"] is False
    assert registry.get("status") == "unreachable", registry
    assert registry["reason"], "the reason still says what was tried"
    assert "api.voice_registry" in str(spy.info.call_args_list), "it is worth a line in the log"
    assert not spy.warning.called, "it is not a failure the user is asked to fix"


def test_archive_does_not_mistake_a_gateway_for_the_sidecar(tmp_path: Path, monkeypatch):
    """A closed loopback port does not reliably refuse the connection.

    On a machine running a proxy in TUN mode, a connect to a port with nothing
    behind it is answered by the proxy -- measured here as an empty-bodied
    ``502`` -- so "an HTTP response arrived" is not evidence that a sidecar is
    running. Only the status the reload route raises for a bad roster is.
    Classifying by response rather than by protocol puts the standing warning
    back on every deployment without a sidecar, which is the bug being fixed.
    """

    spy = MagicMock()
    with _fake_sidecar(status=502) as url:
        registry = _voice_registry_after_archive(tmp_path, monkeypatch, url, log=spy)

    assert registry.get("status") == "unreachable", registry
    assert "HTTP 502" in registry["reason"], "the log still says what answered"
    assert not spy.warning.called


def test_archive_calls_a_slow_reload_unreachable_rather_than_refused(tmp_path: Path, monkeypatch):
    """The path that bites a deployment that has nothing wrong with it.

    ``GsvEngine.reload_voices`` takes the engine lock, so a reload queued behind
    an in-flight synthesis answers late with no sidecar down and no template
    broken. Timing out is not a refusal, and reporting it as one made the
    standing warning a lottery on installs that work.
    """

    spy = MagicMock()
    monkeypatch.setattr("character_memory.api.VOICE_REGISTRY_RELOAD_TIMEOUT_SECONDS", 0.3)
    with _fake_sidecar(status=200, delay=1.5) as url:
        registry = _voice_registry_after_archive(tmp_path, monkeypatch, url, log=spy)

    assert registry["status"] == "unreachable"
    assert "api.voice_registry" in str(spy.info.call_args_list)
    assert not spy.warning.called


def test_the_reload_timeout_stays_a_stall_bound_not_a_wait():
    """The number is a decision, and it has a floor *and* a ceiling.

    The floor is the engine lock: the sidecar holds it across the reload, so the
    refusal worth reporting comes back only once whatever synthesis is in flight
    is done. 1.5s lost that race often enough to be the reported bug.

    The ceiling is the archive button. Nothing about a missed reload is dangerous
    -- the roster is on disk and GSV reads it at its next start -- while this
    timeout is spent on every archive in the deployments that have no sidecar at
    all, measured here at roughly the timeout plus a second before the connect
    gives up. Waiting longer buys no extra truth, only a longer stall.
    """

    from character_memory.api import VOICE_REGISTRY_RELOAD_TIMEOUT_SECONDS

    assert 1.5 <= VOICE_REGISTRY_RELOAD_TIMEOUT_SECONDS <= 5.0
