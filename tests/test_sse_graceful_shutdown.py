from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from character_memory.api import create_api
from character_memory.async_web import attach_async_routes


ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def test_uvicorn_graceful_shutdown_does_not_wait_on_open_sse(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "shutdown.db").as_posix()}"',
                f'persona_path: "{(ROOT / "personas" / "rin" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )

    app = create_api(str(config_path))
    attach_async_routes(app)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
            timeout_graceful_shutdown=2,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        _wait_until(lambda: server.started)
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=httpx.Timeout(5.0)) as client:
            characters = client.get(f"{base}/v1/characters").json()["characters"]
            character_id = characters[0]["id"]
            with client.stream(
                "GET",
                f"{base}/v1/events/stream",
                params={
                    "scope": "direct",
                    "character_id": character_id,
                    "conversation_id": "shutdown-regression",
                },
            ) as response:
                assert response.status_code == 200
                lines = response.iter_lines()
                assert next(lines) == "retry: 1500"

                started = time.monotonic()
                server.should_exit = True
                thread.join(timeout=3.0)
                elapsed = time.monotonic() - started

                assert not thread.is_alive(), "Uvicorn stayed alive waiting for the open SSE response"
                assert elapsed < 3.0
    finally:
        server.should_exit = True
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=2.0)
