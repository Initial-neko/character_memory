import pytest
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import character_memory.api as api_module

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.storage.sqlite import SQLiteStore


def test_web_starts_before_runtime_and_without_api_key(tmp_path, monkeypatch):
    # Settings supports system env > .env > legacy config. This test explicitly
    # exercises the no-key branch, so isolate it from developer/CI environment.
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
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
    assert health.json()["runtime_loading"] is False

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


def test_runtime_route_answers_for_a_character_whose_intent_has_an_embedding(tmp_path, monkeypatch):
    """An embedded intent used to take the whole Runtime drawer down.

    `intents.embedding` is a BLOB, and the route returns whole rows as JSON.
    FastAPI encodes bytes with `.decode()`, so one non-UTF-8 vector raised a
    UnicodeDecodeError and every character that had one answered 500 -- which is
    why this is pinned at the route and not at the listing.
    """
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        "api_key: ''\n"
        "embedding_provider: deterministic\n"
        "embedding_model: deterministic\n"
        f"db_path: '{(tmp_path / 'x.db').as_posix()}'\n"
        "persona_path: personas/rin/persona.yaml\n",
        encoding="utf-8",
    )
    client = TestClient(create_api(str(config)))
    assert client.get("/v1/runtime/rin").status_code == 200

    store = SQLiteStore(tmp_path / "x.db")
    try:
        now = datetime(2026, 9, 26, 12, 0, 0)
        vector = [0.5, -0.25, 1e-09, 3.75]
        # The precondition that makes this a regression test: what the store writes
        # for a real vector is not valid UTF-8. An earlier version of this test passed
        # bytes that `_pack` turned into a decodable float pattern, so it proved
        # nothing -- hence pinning the property instead of trusting the fixture.
        with pytest.raises(UnicodeDecodeError):
            SQLiteStore._pack(vector).decode()
        store.add_intent(
            "rin",
            "把绿萝搬到窗边",
            "none",
            now,
            now,
            now + timedelta(hours=48),
            embedding=vector,
        )
    finally:
        store.close()

    runtime = client.get("/v1/runtime/rin")
    assert runtime.status_code == 200
    assert [item["content"] for item in runtime.json()["intents"]] == ["把绿萝搬到窗边"]
    # The vector stays out of the payload rather than being serialised.
    assert "embedding" not in runtime.json()["intents"][0]



def test_production_eager_warmup_starts_runtime_without_waiting_for_first_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARACTER_MEMORY_EAGER_WARMUP", "1")
    config = tmp_path / "config.yaml"
    config.write_text(
        "api_key: ''\n"
        "embedding_provider: deterministic\n"
        "embedding_model: deterministic\n"
        f"db_path: '{(tmp_path / 'warm.db').as_posix()}'\n"
        "persona_path: personas/rin/persona.yaml\n",
        encoding="utf-8",
    )

    closed = []

    class FakeBundle:
        settings = SimpleNamespace(chat_model="fake", vision_model="fake")
        characters = []
        init_timings = {"total_ms": 1.0}

        def close(self):
            closed.append(True)

    monkeypatch.setattr(api_module, "build_app", lambda _path: FakeBundle())
    app = api_module.create_api(str(config))

    with TestClient(app) as client:
        deadline = time.time() + 2
        health = client.get("/health").json()
        while not health["runtime_loaded"] and time.time() < deadline:
            time.sleep(0.01)
            health = client.get("/health").json()

        assert health["runtime_loaded"] is True
        assert health["runtime_loading"] is False
        assert health["runtime_error"] is None

    assert closed == [True]
