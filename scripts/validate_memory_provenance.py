"""Isolated real HTTP Memory provenance acceptance; no model/network provider calls.

Uses the production API composition and Memory routes with deterministic embeddings.
Only a temporary database/persona/config directory is writable by the fixture.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def request(origin, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(origin + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=15) as response:
        assert response.status == 200
        return json.load(response)


def expect_error(origin, path, status, body=None):
    try:
        request(origin, path, body)
    except HTTPError as error:
        assert error.code == status
    else:
        raise AssertionError(f"expected HTTP {status}: {path}")


@contextmanager
def serving(config, directory):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
           "CHARACTER_MEMORY_DB_PATH": str(directory / "acceptance.db"),
           "CHARACTER_MEMORY_EAGER_WARMUP": "0", "OPENCODE_GO_API_KEY": "isolated-unused-key"}
    command = [sys.executable, str(Path(__file__).resolve()), "--serve", str(config), "--port", str(port)]
    with (directory / "server.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=directory, env=env, stdout=log, stderr=log)
        origin = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError((directory / "server.log").read_text(encoding="utf-8", errors="replace"))
                try:
                    request(origin, "/health")
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("isolated API failed to start")
                    time.sleep(.1)
            yield origin, process.pid
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def fact_snapshot(db):
    with closing(sqlite3.connect(db)) as conn:
        rows = {table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("events", "conversation_events")}
        calls = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='llm_calls'").fetchone()[0]
        if calls:
            calls = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    return {"fact_sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest(),
            "model_calls": calls}


def validate(output):
    from character_memory.domain.models import Event, EventType, Memory
    from character_memory.group_store import GroupEvent, GroupRepository
    from character_memory.storage.sqlite import SQLiteStore

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    evidence = {"schema_version": 1, "commit": commit, "dirty": dirty,
                "api_composition": "create_api + attach_memory_routes", "checks": []}
    with tempfile.TemporaryDirectory(prefix="cm-provenance-") as temporary:
        folder = Path(temporary)
        for name in ("momo", "rin"):
            persona = folder / "personas" / name
            persona.mkdir(parents=True)
            (persona / "persona.yaml").write_text(f"id: {name}\nname: {name}\npersona: 隔离验收人物\n", encoding="utf-8")
        config = folder / "config.yaml"
        config.write_text(json.dumps({"api_key": "isolated-unused-key", "base_url": "http://127.0.0.1:1/v1",
            "embedding_provider": "deterministic", "db_path": str(folder / "acceptance.db"),
            "persona_path": str(folder / "personas/momo/persona.yaml"), "proactive_dispatch_enabled": False}), encoding="utf-8")
        now = datetime.now(timezone.utc)
        store = SQLiteStore(folder / "acceptance.db")
        direct = store.append_event(Event(character_id="momo", event_type=EventType.USER_MESSAGE,
            event_time=now, content="Direct fixture fact"))
        other = store.append_event(Event(character_id="rin", event_type=EventType.USER_MESSAGE,
            event_time=now, content="Other character private fixture"))
        repo = GroupRepository(store)
        group = repo.create_group("HTTP acceptance", ["momo", "rin"], now)
        event = repo.append_event(GroupEvent(conversation_id=group.id, turn_id="http-turn",
            actor_type="USER", actor_id="user", event_type="USER_MESSAGE", event_time=now,
            content="Shared fixture fact"))
        repo.add_trace(group.id, event.turn_id, "momo", event.id, now, {})
        assert event.id == direct.id
        ids = []
        for source_id, metadata in [(direct.id, {}), (None, {"origin": "GROUP", "conversation_id": group.id,
            "source_conversation_event_id": event.id, "turn_id": event.turn_id}), (other.id, {})]:
            ids.append(store.add_memory(Memory(character_id="momo", content="fixture memory", event_time=now,
                source_event_id=source_id, metadata=metadata, embedding=[1.0, 0.0])).id)
        store.close()
        db = folder / "acceptance.db"
        baseline = fact_snapshot(db)
        with serving(config, folder) as (origin, pid):
            evidence["first_process_pid"] = pid
            evidence["first_origin"] = origin
            before_read = fact_snapshot(db)
            first = request(origin, f"/v1/characters/momo/memories/{ids[0]}")["memory"]
            shared = request(origin, f"/v1/characters/momo/memories/{ids[1]}")["memory"]
            blocked = request(origin, f"/v1/characters/momo/memories/{ids[2]}")["memory"]
            assert first["provenance"]["ref"]["kind"] == "EVENT"
            assert shared["provenance"]["ref"]["kind"] == "CONVERSATION_EVENT"
            assert shared["provenance"]["fact"]["content"] == event.content
            assert blocked["source"] is None and blocked["provenance"]["fact"] is None
            expect_error(origin, f"/v1/characters/rin/memories/{ids[0]}", 404)
            expect_error(origin, "/v1/characters/momo/memories/999999", 404)
            assert fact_snapshot(db) == before_read
            evidence["checks"].append("real_http_distinct_domains_readonly_and_owner_boundary")
            path = f"/v1/characters/momo/memories/{ids[1]}"
            for action, body in [("pin", {"pinned": True}), ("active", {"active": False}), ("active", {"active": True})]:
                updated = request(origin, path + "/" + action, body)["memory"]
                assert updated["provenance"] == shared["provenance"]
            corrected = request(origin, path + "/correct", {"content": "Corrected cognition, same original fact"})
            saved = corrected["memory"]
            assert corrected["old_memory"]["provenance"] == saved["provenance"] == shared["provenance"]
            expect_error(origin, path + "/correct", 409, {"content": "must not fork old version"})
            expect_error(origin, path + "/active", 409, {"active": True})
            evidence["checks"].append("real_http_pin_forget_restore_correct_preserve_source_and_conflict_409")
        with serving(config, folder) as (origin, pid):
            evidence["restarted_process_pid"] = pid
            restored = request(origin, f"/v1/characters/momo/memories/{saved['id']}")["memory"]
            old = request(origin, f"/v1/characters/momo/memories/{ids[1]}")["memory"]
            assert restored["provenance"] == saved["provenance"]
            assert old["superseded_by"] == saved["id"] and not old["active"]
            assert fact_snapshot(db) == baseline
            evidence["checks"].append("real_process_restart_retains_correction_and_facts_model_calls_zero")
            evidence["source_ref"] = restored["provenance"]["ref"]
            evidence["final_snapshot"] = fact_snapshot(db)
    evidence["status"] = "PASS"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--serve", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        import uvicorn
        from character_memory.api import create_api
        from character_memory.memory_web import attach_memory_routes
        app = create_api(str(args.serve))
        attach_memory_routes(app)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    else:
        if args.output is None:
            parser.error("--output is required")
        validate(args.output.resolve())
