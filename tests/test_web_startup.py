import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient

from character_memory.api import create_api


def test_web_starts_before_runtime_and_without_api_key(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "api_key: ''\n"
        "embedding_provider: deterministic\n"
        "embedding_model: deterministic\n"
        f"db_path: '{(tmp_path / 'x.db').as_posix()}'\n"
        "persona_path: personas/rin/persona.yaml\n",
        encoding="utf-8",
    )

    app = create_api(str(config))
    client = TestClient(app)

    index = client.get("/")
    assert index.status_code == 200
    assert "Character Memory" in index.text

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["web"] == "ready"
    assert health.json()["runtime_loaded"] is False

    history = client.get("/v1/chat/history")
    assert history.status_code == 200
    assert history.json()["messages"] == []

    runtime = client.get("/v1/runtime/rin")
    assert runtime.status_code == 200
    assert runtime.json()["runtime_loaded"] is False

    health_after_reads = client.get("/health")
    assert health_after_reads.json()["runtime_loaded"] is False

    chat = client.post("/v1/chat", json={"message": "你好"})
    assert chat.status_code == 503
    assert "Missing OPENCODE_GO_API_KEY" in chat.json()["detail"]
