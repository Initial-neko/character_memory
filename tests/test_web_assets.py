"""The pages and their assets must not be servable out of a stale cache.

Nothing sent a ``Cache-Control``, so a browser applied heuristic freshness -- a
fraction of the file's age -- and could run a bundle hours old. That is how a
frontend fix looked like it had not taken effect, twice, on a machine where the
file on disk was already correct.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from character_memory.settings_server import create_settings_app
from character_memory.settings_store import SettingsStore


def _app(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "gsv"\n', encoding="utf-8")
    return create_settings_app(
        str(config), store=SettingsStore(str(config), env_path=str(tmp_path / ".env"))
    )


def test_pages_and_their_assets_revalidate_on_every_load(tmp_path: Path):
    with TestClient(_app(tmp_path)) as client:
        for path in ("/", "/static/app.js", "/static/styles.css"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers.get("cache-control") == "no-cache", path


def test_an_api_answer_is_not_marked_as_the_ui(tmp_path: Path):
    """The rule is about the program, not the data it renders."""

    with TestClient(_app(tmp_path)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers.get("cache-control") is None
