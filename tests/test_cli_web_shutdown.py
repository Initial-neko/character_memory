from __future__ import annotations

import sys
from types import ModuleType

from character_memory import cli


def test_web_cli_bounds_graceful_shutdown(monkeypatch):
    captured = {}
    fake_uvicorn = ModuleType("uvicorn")

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    fake_uvicorn.run = fake_run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    cli._run_server("config.yaml", "127.0.0.1", 8123)

    assert captured["app"] == "character_memory.server:app"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123
    assert captured["reload"] is False
    assert captured["timeout_graceful_shutdown"] == 2
