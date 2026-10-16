"""The retired virtual-life CLI must never construct a runtime or write facts."""
import sys

import pytest

from character_memory import cli
from character_memory.app import AppBundle


@pytest.mark.parametrize("command", ["day", "simulate", "tick"])
def test_simulation_commands_rejected_before_runtime_initialization(monkeypatch, capsys, command):
    def forbidden(*args, **kwargs):
        pytest.fail("retired command attempted runtime initialization")

    monkeypatch.setattr(cli, "build_app", forbidden)
    monkeypatch.setattr(sys, "argv", ["character-memory", command])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_app_bundle_has_no_virtual_life_services():
    assert {"life", "ticker", "days"}.isdisjoint(AppBundle.__dataclass_fields__)



def test_simulation_api_is_absent_without_loading_runtime(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from character_memory import api

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("retired API initialized runtime")

    monkeypatch.setenv("CHARACTER_MEMORY_EAGER_WARMUP", "0")
    monkeypatch.setattr(api, "build_app", forbidden)
    config = tmp_path / "config.yaml"
    database = (tmp_path / "retired-api.db").as_posix()
    config.write_text(f'db_path: "{database}"\nembedding_provider: deterministic\napi_key: ""\n', encoding="utf-8")
    app = api.create_api(str(config))
    with TestClient(app) as client:
        response = client.post("/v1/simulate", json={"days": 1, "character_id": "rin"})
        assert response.status_code == 404
        assert "/v1/simulate" not in client.get("/openapi.json").json()["paths"]
    assert calls == []
