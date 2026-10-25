"""Isolated HTTP + process-crash acceptance for per-Person cognition receipts.

No paid model is used. A local fixed provider counts calls and can hold one
response while its actual API worker is terminated. This does not implement or
claim queue recovery, client retry integration, model quality or visual PASS.
"""
from __future__ import annotations
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def environment(folder):
    return {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
        "OPENCODE_GO_API_KEY": "unused-local-fixture", "CHARACTER_MEMORY_DB_PATH": str(folder / "accept.db"),
        "CHARACTER_MEMORY_EAGER_WARMUP": "0"}


def request(origin, path, body=None):
    req = Request(origin + path, data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=15) as response:
        return response.status, json.load(response)


@contextmanager
def serving(config, folder):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (folder / "api.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", str(config), "--port", str(port)],
            cwd=folder, env=environment(folder), stdout=log, stderr=log)
        origin = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError((folder / "api.log").read_text(encoding="utf-8", errors="replace"))
                try:
                    request(origin, "/health")
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("API failed to start")
                    time.sleep(.1)
            yield origin, process.pid
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def snapshot(db):
    tables = ["events", "conversation_events", "memories", "mental_state_history", "intents",
        "runtime_traces", "conversation_runtime_traces", "conversation_reaction_receipts"]
    with closing(sqlite3.connect(db)) as conn:
        rows = {name: conn.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall() for name in tables}
    return {"counts": {name: len(values) for name, values in rows.items()},
        "sha256": hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()}


def wait_committed(db, reference, characters):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with closing(sqlite3.connect(db)) as conn:
            ready = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_reaction_receipts'").fetchone()
            count = (conn.execute("SELECT COUNT(*) FROM conversation_reaction_receipts WHERE source_ref_json=? AND state='COMMITTED'",
                (reference.model_dump_json(),)).fetchone()[0] if ready else 0)
        if count == characters:
            return
        time.sleep(.1)
    raise TimeoutError("expected cognition commit missing")


def replay(config):
    from character_memory.app import build_app_from_settings
    from character_memory.application.clock import RealClock
    from character_memory.application.group_conversation_service import GroupConversationService
    from character_memory.config import load_settings, discover_character_profiles
    from character_memory.domain.facts import FactRef
    from character_memory.storage.message_receipts import ExecutionUnavailable
    bundle = build_app_from_settings(load_settings(str(config)))
    results = []
    try:
        service = GroupConversationService(bundle.store, bundle.chat.runtime, RealClock(),
            chat_service=bundle.chat, profiles=discover_character_profiles(bundle.settings))
        rows = bundle.store.conn.execute("SELECT source_ref_json,scope FROM message_acceptance_receipts ORDER BY rowid").fetchall()
        for row in rows:
            ref = FactRef.model_validate_json(row["source_ref_json"])
            try:
                if ref.kind == "EVENT":
                    event = bundle.store.get_event(ref.id)
                    result = bundle.chat.runtime[event.character_id].handle(event, persist_event=False)
                    results.append({"source": ref.model_dump(), "replayed": result.replayed, "state": "COMMITTED"})
                else:
                    event = service.repo.get_event(ref.conversation_id, ref.id)
                    result = service.react_from_event(event)
                    results.append({"source": ref.model_dump(), "replayed": all(item.get("replayed") for item in result["decisions"]), "state": "COMMITTED"})
            except ExecutionUnavailable as exc:
                results.append({"source": ref.model_dump(), "state": exc.state, "replayed": False})
    finally:
        bundle.close()
    print(json.dumps({"results": results}, ensure_ascii=False))


def preparation_worker(config, *, recover=False):
    from character_memory.app import build_app_from_settings
    from character_memory.config import load_settings
    from character_memory.domain.models import Event, EventType
    from character_memory.domain.facts import FactRef
    from character_memory.storage.message_receipts import ExecutionUnavailable, ReactionClaim
    bundle = build_app_from_settings(load_settings(str(config)))
    marker = config.with_suffix(".preparation.json")
    try:
        person = bundle.chat.runtime["momo"]
        if not recover:
            event = bundle.store.append_event(Event(character_id="momo", event_type=EventType.USER_MESSAGE,
                event_time=datetime.now(timezone.utc), content="模型前中断后只恢复一次",
                metadata={"conversation_id": "preparation-recovery"}))
            ref = FactRef(kind="EVENT", id=event.id)
            claim = person.execution_receipts.claim(ref, "momo")
            deadline = bundle.store.conn.execute("SELECT preparation_deadline FROM conversation_reaction_receipts "
                "WHERE source_ref_json=? AND character_id='momo'", (ref.model_dump_json(),)).fetchone()[0]
            pending_marker = marker.with_suffix(".tmp")
            pending_marker.write_text(json.dumps({"source_id": event.id, "token": claim.token, "deadline": deadline}), encoding="utf-8")
            pending_marker.replace(marker)
            while True:
                time.sleep(.1)
        data = json.loads(marker.read_text(encoding="utf-8"))
        event = bundle.store.get_event(data["source_id"])
        result = person.handle(event, persist_event=False)
        assert not result.replayed
        before = snapshot(config.parent / "accept.db")
        assert person.handle(event, persist_event=False).replayed
        assert snapshot(config.parent / "accept.db") == before
        old = ReactionClaim(FactRef(kind="EVENT", id=event.id), "momo", "1", data["token"])
        try:
            person.execution_receipts.begin_model(old)
        except ExecutionUnavailable as exc:
            assert exc.state == "LOST_OWNER"
        else:
            raise AssertionError("old preparation owner was not fenced")
        print(json.dumps({"source_id": event.id, "state": "COMMITTED", "replayed_without_writes": True,
            "old_owner_fenced": True}))
    finally:
        bundle.close()


def validate_preparation(config, folder, state):
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["reaction_preparation_lease_seconds"] = 1
    config.write_text(json.dumps(settings), encoding="utf-8")
    before_calls = state["calls"]
    marker = config.with_suffix(".preparation.json")
    with (folder / "preparation.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--prepare", str(config)],
            cwd=folder, env=environment(folder), stdout=log, stderr=log)
        try:
            until = time.monotonic() + 30
            while not marker.exists():
                if process.poll() is not None:
                    raise RuntimeError((folder / "preparation.log").read_text(encoding="utf-8", errors="replace"))
                if time.monotonic() > until:
                    raise TimeoutError("preparation claim was not persisted")
                time.sleep(.05)
            data = json.loads(marker.read_text(encoding="utf-8"))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
    assert state["calls"] == before_calls
    # Use the persisted real server-time deadline, not event/simulated time.
    until = time.monotonic() + 5
    while time.time() <= data["deadline"]:
        if time.monotonic() > until:
            raise TimeoutError("preparation lease did not expire")
        time.sleep(.05)
    recovered = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--recover-preparation", str(config)],
        cwd=folder, env=environment(folder), capture_output=True, text=True, encoding="utf-8", timeout=40)
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout.splitlines()[-1])
    assert state["calls"] == before_calls + 1
    return {**result, "terminated_pid": process.pid, "additional_provider_calls": 1}


def validate(output):
    from character_memory.domain.facts import FactRef
    from character_memory.group_store import GroupRepository
    from character_memory.storage.sqlite import SQLiteStore
    held, release = threading.Event(), threading.Event()
    state = {"hold_next": False, "calls": 0}
    lock = threading.Lock()
    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            with lock:
                state["calls"] += 1
                hold = state["hold_next"]
                state["hold_next"] = False
            if hold:
                held.set()
                if not release.wait(30):
                    return
            value = {"actions": [{"type": "MESSAGE", "message": "一次测试回应"}],
                "mental_state_update": "记住测试交互", "memory_candidates": [{"content": "一次测试共同经历", "memory_type": "SHARED", "importance": .8}]}
            data = json.dumps({"id": "local-fixture", "object": "chat.completion", "created": 0,
                "model": "fixed-local", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(value)}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            try:
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            except OSError:
                if not hold:
                    raise
        def log_message(self, *args):
            pass
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    evidence = {"schema_version": 1, "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
        "paid_model_calls": 0, "checks": []}
    try:
        with tempfile.TemporaryDirectory(prefix="cm-cognition-receipts-") as temporary:
            folder = Path(temporary)
            for name in ("momo", "rin"):
                path = folder / "personas" / name
                path.mkdir(parents=True)
                (path / "persona.yaml").write_text(f"id: {name}\nname: {name}\n", encoding="utf-8")
            config = folder / "config.yaml"
            config.write_text(json.dumps({"api_key": "unused-local-fixture", "base_url": f"http://127.0.0.1:{provider.server_port}/v1",
                "embedding_provider": "deterministic", "db_path": str(folder / "accept.db"),
                "persona_path": str(folder / "personas/momo/persona.yaml"), "proactive_dispatch_enabled": False, "llm_attempts": 1}), encoding="utf-8")
            store = SQLiteStore(folder / "accept.db")
            group = GroupRepository(store).create_group("cognition receipts", ["momo", "rin"], datetime.now(timezone.utc))
            store.close()
            from PIL import Image
            from io import BytesIO
            import base64
            image = BytesIO(); Image.new("RGB", (3, 3), "red").save(image, format="PNG")
            attachment = {"filename": "fixture.png", "data_url": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()}
            with serving(config, folder) as (origin, pid):
                evidence["http_pid"] = pid
                for kind in ("EVENT", "CONVERSATION_EVENT"):
                    for attached in (False, True):
                        body = {"request_id": f"{kind}-{attached}", "message": "只处理同一来源一次"}
                        if kind == "EVENT":
                            body.update(character_id="momo", conversation_id="receipt-validation")
                        if attached:
                            body["image"] = attachment
                        url = "/v1/chat/messages" if kind == "EVENT" else f"/v1/groups/{group.id}/messages"
                        status, result = request(origin, url, body)
                        assert status == 202
                        ref = FactRef(kind=kind, id=result["event_id"], conversation_id=None if kind == "EVENT" else group.id)
                        wait_committed(folder / "accept.db", ref, 1 if kind == "EVENT" else 2)
                assert state["calls"] == 6
                evidence["checks"].append("production_http_direct_group_text_image_six_person_commits")
                with lock:
                    state["hold_next"] = True
                status, interrupted = request(origin, "/v1/chat/messages", {"request_id": "interrupted", "character_id": "momo",
                    "conversation_id": "receipt-validation", "message": "中断后不应重复调用"})
                assert status == 202 and held.wait(10)
                evidence["interrupted_source_id"] = interrupted["event_id"]
                assert state["calls"] == 7
                # Exiting serving terminates the real worker while Provider holds.
            release.set()
            before = snapshot(folder / "accept.db")
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--replay", str(config)],
                cwd=folder, env=environment(folder), capture_output=True, text=True, encoding="utf-8", timeout=40)
            assert process.returncode == 0, process.stderr
            results = json.loads(process.stdout.splitlines()[-1])["results"]
            assert len(results) == 5
            assert sum(item["state"] == "COMMITTED" and item["replayed"] for item in results) == 4
            blocked = [item for item in results if item["state"] != "COMMITTED"]
            assert len(blocked) == 1 and blocked[0]["state"] in {"CALLING", "UNKNOWN"}
            assert state["calls"] == 7
            after = snapshot(folder / "accept.db")
            assert before == after
            evidence.update(status="PASS", replay_results=results, provider_calls=state["calls"], snapshot=after)
            evidence["checks"].extend(["fresh_process_reuses_committed_cognition_without_calls_or_writes", "terminated_provider_inflight_worker_is_not_reexecuted"])
            evidence["preparation_recovery"] = validate_preparation(config, folder, state)
            evidence["provider_calls"] = state["calls"]
            evidence["checks"].append("terminated_pre_model_worker_recovered_once_after_real_deadline")
    finally:
        release.set(); provider.shutdown(); provider.server_close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--serve", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--replay", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--prepare", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--recover-preparation", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        import uvicorn
        from character_memory.api import create_api
        from character_memory.async_web import attach_async_routes
        from character_memory.group_web import attach_group_routes
        app = create_api(str(args.serve)); attach_group_routes(app, str(args.serve)); attach_async_routes(app)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    elif args.replay:
        replay(args.replay)
    elif args.prepare:
        preparation_worker(args.prepare)
    elif args.recover_preparation:
        preparation_worker(args.recover_preparation, recover=True)
    else:
        if args.output is None:
            parser.error("--output is required")
        validate(args.output.resolve())
