"""Real HTTP acceptance for Activity lifecycle, restart and zero model calls.

A temp SQLite database and temp persona root back real uvicorn processes; the
deterministic model writes one JSON line per reaction so "zero model calls" is an
observed count, not an assumption.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import httpx


ROOT = Path(__file__).resolve().parents[1]
PERSONAS = ("rin", "momo")


def _write_personas(root: Path) -> None:
    for name in PERSONAS:
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "persona.yaml").write_text(
            f"id: {name}\nname: {name.title()}\nidentity: e2e activity character\n",
            encoding="utf-8",
        )


@contextmanager
def server(root: Path):
    _write_personas(root / "personas")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "CHARACTER_MEMORY_ACTIVITY_DB": str(root / "activity.db"),
        "CHARACTER_MEMORY_ACTIVITY_MEDIA": str(root / "media"),
        "CHARACTER_MEMORY_ACTIVITY_PERSONAS": str(root / "personas"),
        "ACTIVITY_MODEL_CALL_LOG": str(root / "model-calls.jsonl"),
    }
    options = {}
    tree = None
    if os.name == "nt":
        from character_memory.runtime.process_tree import WindowsProcessTree

        tree = WindowsProcessTree()
        options["creationflags"] = 0x00000004 | 0x08000000
    with (root / f"api-{port}.log").open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "e2e_activity_app:app",
             "--app-dir", str(ROOT / "tests"), "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=log, stderr=log, **options,
        )
        try:
            if tree:
                tree.attach_and_resume(proc)
            origin = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError((root / f"api-{port}.log").read_text(
                        encoding="utf-8", errors="replace"))
                try:
                    with urlopen(origin + "/health", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("activity HTTP readiness timeout")
            with httpx.Client(base_url=origin, timeout=20) as client:
                yield client
        finally:
            if tree:
                tree.close()
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)


def _create_body(request_id="req-activity-0001"):
    return {
        "request_id": request_id,
        "title": "周末一起整理旧照片",
        "participants": [
            {"actor_type": "CHARACTER", "character_id": "rin"},
            {"actor_type": "CHARACTER", "character_id": "momo"},
            {"actor_type": "SIMULATED_CHARACTER", "local_id": "neighbor", "display_name": "隔壁邻居"},
        ],
    }


def _model_calls(root: Path) -> list[dict]:
    log = root / "model-calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_activity_lifecycle_over_real_http_survives_restart(tmp_path):
    pids = []
    with server(tmp_path) as client:
        pids.append(client.get("/__e2e/activity-runtime").json()["pid"])
        created = client.post("/v2/activities", json=_create_body())
        assert created.status_code == 201, created.text
        activity = created.json()["activity"]
        activity_id = activity["id"]
        assert activity["status"] == "CREATED" and activity["version"] == 1
        assert created.json()["replayed"] is False
        assert activity["experience_scope"] == {
            "schema_version": 1, "reality": "SIMULATED", "activity_id": activity_id}
        assert [item["actor_type"] for item in activity["participants"]] == [
            "CHARACTER", "CHARACTER", "SIMULATED_CHARACTER"]

        # A simulated participant is not a formal Person and not a sidebar entry.
        listed = {item["id"] for item in client.get("/v1/characters").json()["characters"]}
        assert listed == set(PERSONAS)
        assert "neighbor" not in listed

        read = client.get(f"/v2/activities/{activity_id}")
        assert read.status_code == 200 and read.json()["participants"] == activity["participants"]
        assert client.get("/v2/activities").json()["activities"][0]["id"] == activity_id

        def action(version, request_id, operation):
            return client.post(f"/v2/activities/{activity_id}/{operation}",
                               json={"request_id": request_id, "expected_version": version})

        resumed = action(1, "req-resume-0001", "resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["activity"]["status"] == "ACTIVE"
        paused = action(2, "req-pause-0001", "pause")
        assert paused.json()["activity"]["status"] == "PAUSED"
        stale = action(2, "req-stale-0001", "resume")
        assert stale.status_code == 409 and stale.json()["detail"] == "ACTIVITY_VERSION_CONFLICT"
        replay = action(2, "req-pause-0001", "pause")
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["activity"] == paused.json()["activity"]
        resumed_again = action(3, "req-resume-0002", "resume")
        assert resumed_again.json()["activity"]["status"] == "ACTIVE"
        version_before_restart = resumed_again.json()["activity"]["version"]

        # Creation rejects unknown formal participants and a cast with nobody real.
        unknown = client.post("/v2/activities", json={
            "request_id": "req-activity-unknown",
            "title": "不存在的参与者",
            "participants": [{"actor_type": "CHARACTER", "character_id": "nobody"}],
        })
        assert unknown.status_code == 422
        simulated_only = client.post("/v2/activities", json={
            "request_id": "req-activity-simonly",
            "title": "只有模拟人物",
            "participants": [{"actor_type": "SIMULATED_CHARACTER", "local_id": "ghost",
                              "display_name": "幽灵"}],
        })
        assert simulated_only.status_code == 422

    assert _model_calls(tmp_path) == [], "activity lifecycle must not call the model"

    with server(tmp_path) as client:
        pids.append(client.get("/__e2e/activity-runtime").json()["pid"])
        restored = client.get(f"/v2/activities/{activity_id}")
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "ACTIVE"
        assert restored.json()["version"] == version_before_restart
        assert restored.json()["participants"][2]["display_name"] == "隔壁邻居"
        # Idempotent lifecycle work keeps working after a process restart.
        replay = client.post(f"/v2/activities/{activity_id}/resume",
                             json={"request_id": "req-resume-0002", "expected_version": 3})
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        paused = client.post(f"/v2/activities/{activity_id}/pause",
                             json={"request_id": "req-pause-after-restart", "expected_version": 4})
        assert paused.status_code == 200 and paused.json()["activity"]["status"] == "PAUSED"
        ended = client.post(f"/v2/activities/{activity_id}/end",
                            json={"request_id": "req-end-0001", "expected_version": 5})
        assert ended.status_code == 200 and ended.json()["activity"]["status"] == "ENDED"
        refused = client.post(f"/v2/activities/{activity_id}/resume",
                              json={"request_id": "req-after-end-01", "expected_version": 6})
        assert refused.status_code == 409 and refused.json()["detail"] == "ACTIVITY_STATE_CONFLICT"

    assert _model_calls(tmp_path) == [], "restart, reads and resumes must not call the model"
    assert len(set(pids)) == 2


def test_production_composition_exposes_activity_without_runtime(tmp_path):
    """The route must be registered by server.py, not only by a test app."""
    from fastapi.testclient import TestClient

    from character_memory.server import create_server_app

    _write_personas(tmp_path / "personas")
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            'api_key: ""',
            'embedding_provider: "deterministic"',
            f'db_path: "{(tmp_path / "server.db").as_posix()}"',
            f'media_dir: "{(tmp_path / "media").as_posix()}"',
            f'persona_path: "{(tmp_path / "personas" / "rin" / "persona.yaml").as_posix()}"',
            "proactive_dispatch_enabled: false",
        ]),
        encoding="utf-8",
    )
    app = create_server_app(str(config))
    with TestClient(app) as client:
        created = client.post("/v2/activities", json=_create_body())
        assert created.status_code == 201, created.text
        activity_id = created.json()["activity"]["id"]
        read = client.get(f"/v2/activities/{activity_id}")
        assert read.status_code == 200
        assert read.json()["status"] == "CREATED"
        assert client.get("/health").json()["runtime_loaded"] is False
        paused = client.post(f"/v2/activities/{activity_id}/pause",
                             json={"request_id": "req-pause-prod", "expected_version": 1})
        assert paused.status_code == 409 and paused.json()["detail"] == "ACTIVITY_STATE_CONFLICT"

