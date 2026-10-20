"""Offline contract candidate: archive while a queued Space decision is running.

Only fixture config/personas/SQLite below pytest tmp_path are modified.
Model and embedding are deterministic fakes; no production runtime is started.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import os
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# Portable both from this ignored review location and once adopted under tests/.
ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
sys.path.insert(0, str(ROOT / "tests"))
from test_character_space import _config
from test_space_autonomy import SpaceModel
from character_memory.api import create_api
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.space_web import attach_space_routes


class BlockingModel(SpaceModel):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def react_call_for_session(self, context, session_id):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10), "test never released fake model"
        return super().react_call_for_session(context, session_id)


class LateArchivedSpaceReply(AssertionError):
    """Only the proven publication-after-archive condition is expected to fail."""


@pytest.mark.parametrize("path", ["normal", "explicit_mention"])
@pytest.mark.parametrize("phase", ["active", "archive_before", "archive_during"])
def test_archived_role_cannot_publish_late_space_reply(tmp_path, monkeypatch, path, phase):
    network_attempts = []
    def deny_network(sock, address):
        network_attempts.append(str(address))
        raise AssertionError("offline evidence must not connect to a real service")
    monkeypatch.setenv("CHARACTER_MEMORY_EAGER_WARMUP", "0")
    config = _config(tmp_path, count=2)
    with config.open("a", encoding="utf-8") as file:
        file.write("\ntts_provider: edge\nproactive_dispatch_enabled: false\nspace_autonomy_enabled: false\n")
    app = create_api(str(config))
    attach_space_routes(app)
    access = app.state.character_memory
    model = BlockingModel()
    embedding = DeterministicEmbedding()
    runtime = PersonRuntime(access.read_store, VectorRecall(access.read_store, embedding), embedding, model, "persona c01")
    # Keep real HTTP routes, Space queue/service, PersonRuntime and SQLite.
    # Replace only lazy provider construction with the isolated fake bundle.
    fake_bundle_calls = []
    def require_fake_bundle():
        fake_bundle_calls.append("fake bundle")
        return SimpleNamespace(runtimes={"c01": runtime})
    access.require_bundle = require_fake_bundle
    trace = []
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        # Windows creates a localhost socketpair for the ASGI portal itself.
        # Deny provider/network connections only after that internal setup.
        monkeypatch.setattr(socket.socket, "connect", deny_network)
        # attach/start saw api_key="", so no autonomous scheduler thread starts.
        access.settings.api_key = "offline-fake-not-a-provider-key"
        access.settings.space_thread_reply_rounds = 1
        # This historical text fixture tests archive/reaction races, not new
        # publication, which now requires an image.
        post_id = access.space_repository.create_post("c01", "fixture post", datetime.now(timezone.utc)).id
        body = {"content": "fixture user comment", "client_request_id": f"{path}-{phase}"}
        if path == "explicit_mention":
            body["mentions"] = ["c01"]
        response = client.post(f"/v1/space/posts/{post_id}/comments", json=body)
        assert response.status_code == 200
        user_comment_id = response.json()["comment"]["id"]
        trace.append({"step": "comment_stored", "id": user_comment_id, "queued": access.space_scheduler.pending_thread_count(), "comments": response.json()["post"]["comments"], "notifications": client.get("/v1/space/notifications").json()})

        def archive():
            result = client.post("/v1/characters/c01/archive")
            assert result.status_code == 200, result.text
            profiles = {item["id"]: item for item in access.character_profiles()}
            assert "archived_at" in profiles["c01"]
            comments = client.get(f"/v1/space/posts/{post_id}").json()["post"]["comments"]
            assert [item["id"] for item in comments] == [user_comment_id]
            trace.append({"step": "archive_http_200_before_reply_commit", "http_status": result.status_code, "archived_at": profiles["c01"]["archived_at"], "comment_ids": [item["id"] for item in comments], "notifications": client.get("/v1/space/notifications").json()})

        if phase == "archive_before":
            archive()
        if phase == "archive_during":
            future = pool.submit(access.space_scheduler.drain_pending_threads)
            try:
                assert model.entered.wait(5), "queued reaction never reached fake model"
                trace.append({"step": "fake_model_entered", "calls": model.calls})
                archive()
            finally:
                model.release.set()
            result = future.result(timeout=10)
        else:
            model.release.set()
            result = access.space_scheduler.drain_pending_threads()
        comments = client.get(f"/v1/space/posts/{post_id}").json()["post"]["comments"]
        role_comments = [item for item in comments if item["actor_type"] == "CHARACTER"]
        inbox = client.get("/v1/space/notifications").json()
        trace.append({"step": "worker_finished", "reply_ids": [item["id"] for item in result], "role_comments": role_comments, "notifications": inbox, "unread_count": inbox["unread_count"], "model_calls": model.calls})
        events = access.read_store.list_events("c01", limit=20, event_type="SPACE_COMMENT_RECEIVED")
        assert len(events) == (0 if phase == "archive_before" else 1)
        assert model.calls == (0 if phase == "archive_before" else 1)
        assert network_attempts == []
        assert client.get("/health").json()["runtime_loaded"] is False
        source_paths = ["src/character_memory/space_autonomy.py", "src/character_memory/space_store.py", "src/character_memory/core_character_web.py", "src/character_memory/api_character_service.py", "src/character_memory/runtime/person_runtime.py"]
        evidence = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_paths}, "path": path, "phase": phase, "trace": trace, "fake_bundle_calls": len(fake_bundle_calls), "network_attempts": network_attempts, "network_guard_scope": "socket.connect denied only after TestClient/asyncio portal startup; startup config has empty api_key and eager warmup disabled", "runtime_loaded": False, "person_runtime_event_ids": [item.id for item in events]}
        evidence_dir = Path(os.environ.get("SPACE_ARCHIVE_EVIDENCE_DIR", str(ROOT / "artifacts/core-visual-evidence/space-archive-inflight")))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / f"archive-{path}-{phase}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        if phase == "archive_during" and role_comments:
            # Keep apparatus/lifecycle/provider errors outside the narrow XFAIL.
            assert len(role_comments) == 1
            assert role_comments[0]["author"]["archived"] is True
            assert role_comments[0]["reply_to_comment_id"] == user_comment_id
            assert inbox["unread_count"] == 1
            assert any(item["step"] == "archive_http_200_before_reply_commit" for item in trace)
            raise LateArchivedSpaceReply("archive HTTP 200 and an empty reply set precede a newly committed public reply and unread notification")
        assert len(role_comments) == (1 if phase == "active" else 0)
        assert model.calls == (0 if phase == "archive_before" else 1)


def test_space_publish_guard_uses_the_production_character_lock(tmp_path, monkeypatch):
    """The publish re-check must share archive/restore's real lock."""
    monkeypatch.setenv("CHARACTER_MEMORY_EAGER_WARMUP", "0")
    config = _config(tmp_path, count=2)
    app = create_api(str(config))
    attach_space_routes(app)
    access = app.state.character_memory
    assert hasattr(access, "character_write_lock")
    assert access.space_autonomy._archive_guard() is access.character_write_lock

