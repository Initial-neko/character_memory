from pathlib import Path

import pytest

from character_memory.web_lifecycle import on_app_event


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "character_memory"


def test_core_api_reports_the_installed_package_version():
    """The API version must be the installed one, not a separate string.

    A hardcoded version had drifted far behind pyproject.toml, so /openapi.json
    and every client reading it disagreed with what was actually running.
    """
    from importlib.metadata import version as package_version

    from character_memory.api import _package_version

    assert _package_version() == package_version("character-memory")


def test_no_runtime_module_uses_deprecated_fastapi_on_event_decorator():
    offenders = []
    for path in SRC.rglob("*.py"):
        if "@app.on_event(" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_lifecycle_registration_preserves_startup_and_shutdown_callbacks():
    class Router:
        def __init__(self):
            self.calls = []

        def add_event_handler(self, event_type, func):
            self.calls.append((event_type, func))

    class App:
        def __init__(self):
            self.router = Router()

    app = App()

    @on_app_event(app, "startup")
    def start():
        return "started"

    @on_app_event(app, "shutdown")
    def stop():
        return "stopped"

    assert [(event, func.__name__) for event, func in app.router.calls] == [
        ("startup", "start"),
        ("shutdown", "stop"),
    ]
    assert start() == "started"
    assert stop() == "stopped"


def test_release_candidate_version_and_policy_are_pinned():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    releases = (ROOT / "docs" / "current" / "RELEASES.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert 'version = "0.5.0rc1"' in pyproject
    assert "v0.5.0-rc.1" in releases
    assert "0.5.0-rc.1" in changelog
    assert "stable" in releases.lower()
