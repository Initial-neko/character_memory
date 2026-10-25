"""W2 A: accepted facts are idempotent; execution recovery is a separate gate."""
from datetime import datetime, timezone
import base64
from io import BytesIO
import json

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.async_web import attach_async_routes
from character_memory.group_store import GroupRepository


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    for name in ("momo", "rin"):
        path = tmp_path / "personas" / name
        path.mkdir(parents=True)
        (path / "persona.yaml").write_text(f"id: {name}\nname: {name}\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(json.dumps({"api_key": "", "embedding_provider": "deterministic",
        "db_path": str(tmp_path / "accept.db"), "proactive_dispatch_enabled": False,
        "persona_path": str(tmp_path / "personas/momo/persona.yaml")}), encoding="utf-8")
    apps, calls = [], []
    def open_app():
        app = create_api(str(config))
        attach_async_routes(app)
        access = app.state.character_memory
        store = access.store()
        def dispatch(*args, **kwargs):
            assert not store.conn.in_transaction, "dispatch must happen after durable commit"
            calls.append((args, kwargs))
        monkeypatch.setattr(access.reaction_scheduler, "enqueue_direct", dispatch)
        monkeypatch.setattr(access.reaction_scheduler, "enqueue_group", dispatch)
        apps.append(app)
        return TestClient(app), store
    client, store = open_app()
    group = GroupRepository(store).create_group("receipt test", ["momo", "rin"], datetime.now(timezone.utc))
    yield client, store, group, calls, open_app
    for app in apps:
        app.state.character_memory.stream_hub.close()
        app.state.character_memory.read_store.close()


def endpoint(group, scope):
    return "/v1/chat/messages" if scope == "DIRECT" else f"/v1/groups/{group.id}/messages"


def body(scope, *, image=False):
    result = {"request_id": "stable-request", "message": "原始消息"}
    if scope == "DIRECT":
        result.update(character_id="momo", conversation_id="one")
    if image:
        buffer = BytesIO()
        Image.new("RGB", (3, 3), "red").save(buffer, format="PNG")
        result["image"] = {"filename": "fixture.png", "data_url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()}
    return result


@pytest.mark.parametrize("scope", ["DIRECT", "GROUP"])
@pytest.mark.parametrize("image", [False, True])
def test_keyed_acceptance_reuses_fact_media_and_dispatch_after_restart(acceptance, scope, image):
    client, store, group, calls, open_app = acceptance
    url, data = endpoint(group, scope), body(scope, image=image)
    first = client.post(url, json=data)
    second = client.post(url, json=data)
    assert first.status_code == second.status_code == 202
    assert first.json()["event_id"] == second.json()["event_id"]
    assert second.json()["request_receipt"]["replayed"] is True
    assert second.json()["request_receipt"]["execution"] == "NOT_TRACKED"
    restarted, _ = open_app()
    third = restarted.post(url, json=data)
    assert third.status_code == 202
    assert third.json()["event_id"] == first.json()["event_id"]
    assert len(calls) == 1
    table = "events" if scope == "DIRECT" else "conversation_events"
    assert store.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE event_type='USER_MESSAGE'").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == int(image)


@pytest.mark.parametrize("scope", ["DIRECT", "GROUP"])
def test_same_request_key_with_changed_payload_is_conflict_before_dispatch(acceptance, scope):
    client, store, group, calls, _ = acceptance
    url, data = endpoint(group, scope), body(scope)
    assert client.post(url, json=data).status_code == 202
    response = client.post(url, json={**data, "message": "changed"})
    assert response.status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize("scope", ["DIRECT", "GROUP"])
@pytest.mark.parametrize("key", ["", " ", "\t", 123, "x" * 129])
def test_invalid_keys_fail_before_acceptance(acceptance, scope, key):
    client, store, group, calls, _ = acceptance
    response = client.post(endpoint(group, scope), json={**body(scope), "request_id": key})
    assert response.status_code == 422
    assert calls == []


def test_legacy_messages_without_keys_remain_independent(acceptance):
    client, store, group, calls, _ = acceptance
    data = body("DIRECT")
    del data["request_id"]
    first = client.post(endpoint(group, "DIRECT"), json=data).json()
    second = client.post(endpoint(group, "DIRECT"), json=data).json()
    assert first["event_id"] != second["event_id"]
    assert "request_receipt" not in first
    assert len(calls) == 2


def test_same_key_in_different_conversations_is_not_a_collision(acceptance):
    client, store, group, calls, _ = acceptance
    data = body("DIRECT")
    first = client.post(endpoint(group, "DIRECT"), json=data).json()
    second = client.post(endpoint(group, "DIRECT"), json={**data, "conversation_id": "two"}).json()
    assert first["event_id"] != second["event_id"]
    assert len(calls) == 2


def test_failed_queue_handoff_is_unknown_and_replay_does_not_execute_again(acceptance, monkeypatch):
    client, store, group, calls, _ = acceptance
    def broken(*args, **kwargs):
        calls.append("attempt")
        raise RuntimeError("queue handoff interrupted")
    monkeypatch.setattr(client.app.state.character_memory.reaction_scheduler, "enqueue_direct", broken)
    with pytest.raises(RuntimeError, match="handoff interrupted"):
        client.post(endpoint(group, "DIRECT"), json=body("DIRECT"))
    replay = client.post(endpoint(group, "DIRECT"), json=body("DIRECT"))
    assert replay.status_code == 202
    assert replay.json()["request_receipt"]["dispatch"] == "UNKNOWN"
    assert replay.json()["request_receipt"]["execution"] == "NOT_TRACKED"
    assert calls == ["attempt"]
    assert store.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='USER_MESSAGE'").fetchone()[0] == 1


def test_acceptance_failure_rolls_back_fact_receipt_and_staged_media(acceptance, monkeypatch):
    from pathlib import Path
    client, store, group, calls, _ = acceptance
    def broken(event):
        raise RuntimeError("injected fact write failure")
    monkeypatch.setattr(store, "append_event", broken)
    with pytest.raises(RuntimeError, match="fact write failure"):
        client.post(endpoint(group, "DIRECT"), json=body("DIRECT", image=True))
    assert store.conn.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM message_acceptance_receipts").fetchone()[0] == 0
    assert not [p for p in (Path(store.path).parent / "media").rglob("*") if p.is_file()]
    assert calls == []


def test_two_processes_accept_one_fact_under_the_same_key(acceptance):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time
    client, store, group, calls, _ = acceptance
    source = """
import json,sys,time
from datetime import datetime,timezone
from character_memory.domain.models import Event,EventType
from character_memory.storage.sqlite import SQLiteStore
from character_memory.storage.message_receipts import MessageReceiptRepository
store=SQLiteStore(sys.argv[1]); repo=MessageReceiptRepository(store)
while time.time()<float(sys.argv[2]): time.sleep(.01)
def create():
 e=store.append_event(Event(character_id='momo',event_type=EventType.USER_MESSAGE,event_time=datetime.now(timezone.utc),content='once'))
 return {'accepted':True,'event_id':e.id}
a=repo.accept(['DIRECT','momo','cross-process'],'cross-process-key',{'message':'once'},create,source_kind='EVENT')
print(json.dumps({'created':a.created,'event_id':a.response['event_id']}));store.close()
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONIOENCODING": "utf-8"}
    command = [sys.executable, "-c", source, str(store.path), str(time.time() + 3)]
    processes = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for _ in range(2)]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert sorted(row["created"] for row in results) == [False, True]
    assert len({row["event_id"] for row in results}) == 1
    assert store.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='USER_MESSAGE'").fetchone()[0] == 1
