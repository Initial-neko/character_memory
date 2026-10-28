"""Real-HTTP acceptance for P13 DSH authorization.

Two independent uvicorn processes share one temporary SQLite database: the
operator/Main listener (admin routes + activities) and the DSH listener
(business routes only). Every assertion below talks to those processes or to a
second SQLite connection; nothing is mocked at the service boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode
from urllib.request import urlopen

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "dsh-operator-token-for-acceptance"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _write_personas(root: Path) -> None:
    for name in ("rin", "momo"):
        target = root / "personas" / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "persona.yaml").write_text(
            f"id: {name}\nname: {name.title()}\nidentity: e2e dsh character\n",
            encoding="utf-8",
        )


def _config(root: Path) -> Path:
    _write_personas(root)
    path = root / "config.yaml"
    path.write_text(
        "\n".join([
            'api_key: ""',
            'embedding_provider: "deterministic"',
            f'db_path: "{(root / "activity.db").as_posix()}"',
            f'media_dir: "{(root / "media").as_posix()}"',
            f'persona_path: "{(root / "personas" / "rin" / "persona.yaml").as_posix()}"',
            "proactive_dispatch_enabled: false",
        ]),
        encoding="utf-8",
    )
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _spawn(name: str, module: str, port: int, env: dict, root: Path):
    options = {}
    tree = None
    if os.name == "nt":
        from character_memory.runtime.process_tree import WindowsProcessTree

        tree = WindowsProcessTree()
        options["creationflags"] = 0x00000004 | 0x08000000
    log = (root / f"{name}-{port}.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", f"{module}:app",
         "--app-dir", str(ROOT / "tests"), "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=log, **options,
    )
    return proc, tree, log


def _wait_health(origin: str, proc, log_path: Path) -> None:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))
        try:
            with urlopen(origin + "/health", timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"readiness timeout for {origin}")


class Stack:
    def __init__(self, root: Path):
        self.root = root
        self.config = _config(root)
        self.operator_port = _free_port()
        self.dsh_port = _free_port()
        self.operator = f"http://127.0.0.1:{self.operator_port}"
        self.dsh = f"http://127.0.0.1:{self.dsh_port}"
        self.processes = []

    def start(self):
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
            "CHARACTER_MEMORY_CONFIG": str(self.config),
            "CHARACTER_MEMORY_DSH_OPERATOR_TOKEN": TOKEN,
            "CHARACTER_MEMORY_EAGER_WARMUP": "0",
        }
        for name, module, port in (
            ("operator", "e2e_dsh_operator_app", self.operator_port),
            ("dsh", "e2e_dsh_agent_app", self.dsh_port),
        ):
            proc, tree, log = _spawn(name, module, port, env, self.root)
            if tree:
                tree.attach_and_resume(proc)
            self.processes.append((proc, tree, log))
        _wait_health(self.operator, self.processes[0][0],
                     self.root / f"operator-{self.operator_port}.log")
        _wait_health(self.dsh, self.processes[1][0],
                     self.root / f"dsh-{self.dsh_port}.log")
        return self

    def stop(self):
        for proc, tree, log in reversed(self.processes):
            try:
                if tree:
                    tree.close()
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            finally:
                log.close()
        self.processes = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def dsh_db(self):
        connection = sqlite3.connect(self.root / "activity.db", timeout=15)
        connection.row_factory = sqlite3.Row
        return connection


def _operator_admin(client: httpx.Client, method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["X-DSH-Operator-Token"] = TOKEN
    return client.request(method, path, headers=headers, **kwargs)


class SignedClient:
    def __init__(self, stack: Stack, subject_id: str, key_id: str,
                 private_key: str):
        self.stack = stack
        self.subject_id = subject_id
        self.key_id = key_id
        self.private_key = private_key

    def _next_nonce(self) -> str:
        # No shared counter: two threads may call prepare() concurrently.
        return secrets.token_urlsafe(18)

    def prepare(self, method: str, path: str, *, grant_id: str = "",
                params: dict | None = None, payload: dict | None = None):
        query = urlencode(params or {})
        path_with_query = path + (f"?{query}" if query else "")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() \
            if payload is not None else b""
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        nonce = self._next_nonce()
        message = "\n".join([
            "DSH1", method.upper(), path_with_query,
            hashlib.sha256(body).hexdigest(), timestamp, nonce, grant_id,
        ]).encode("utf-8")
        private = Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(
            self.private_key + "=" * (-len(self.private_key) % 4)))
        signature = _b64(private.sign(message))
        headers = {
            "X-DSH-Subject": self.subject_id,
            "X-DSH-Key": self.key_id,
            "X-DSH-Timestamp": timestamp,
            "X-DSH-Nonce": nonce,
            "X-DSH-Signature": signature,
            "Content-Type": "application/json",
        }
        return {
            "method": method, "path": path_with_query, "body": body,
            "headers": headers,
        }

    def send(self, prepared, *, origin: str | None = None,
             client: httpx.Client | None = None) -> httpx.Response:
        own = client is None
        client = client or httpx.Client(timeout=30)
        try:
            return client.request(
                prepared["method"], (origin or self.stack.dsh) + prepared["path"],
                content=prepared["body"] or None, headers=prepared["headers"],
            )
        finally:
            if own:
                client.close()

    def get(self, path: str, *, grant_id: str = "", params: dict | None = None):
        return self.send(self.prepare("GET", path, grant_id=grant_id, params=params))

    def post(self, path: str, *, grant_id: str = "", params: dict | None = None,
             payload: dict | None = None):
        return self.send(self.prepare(
            "POST", path, grant_id=grant_id, params=params, payload=payload))


def _error(response: httpx.Response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code, detail
    return detail


def _grant_body(request_id: str, subject_id: str, activity_id: str, *,
                scopes: list[str], actions: list[str] | None = None,
                expires_at: str | None = None, max_reads: int = 200,
                max_actions: int = 20, public_facts: list[dict] | None = None):
    return {
        "request_id": request_id,
        "subject_id": subject_id,
        "activity_id": activity_id,
        "scopes": scopes,
        "actions": actions if actions is not None else [],
        "public_facts": public_facts or [],
        "expires_at": expires_at or (
            datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "max_reads": max_reads,
        "max_actions": max_actions,
    }


def _activity_body(request_id: str, title: str, subject_id: str, character="rin"):
    return {
        "request_id": request_id,
        "title": title,
        "participants": [
            {"actor_type": "CHARACTER", "character_id": character},
            {"actor_type": "DSH", "subject_id": subject_id},
        ],
    }


def _create_activity(operator: httpx.Client, subject_id: str, request_id: str,
                     title: str, character="rin", resume=True) -> str:
    response = operator.post("/v2/activities",
                             json=_activity_body(request_id, title, subject_id, character))
    assert response.status_code == 201, response.text
    activity = response.json()["activity"]
    assert any(item["actor_type"] == "DSH" and item["subject_id"] == subject_id
               for item in activity["participants"])
    if resume:
        resumed = operator.post(f"/v2/activities/{activity['id']}/resume",
                                json={"request_id": request_id + "-resume",
                                      "expected_version": activity["version"]})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["activity"]["status"] == "ACTIVE"
    return activity["id"]


@contextmanager
def signed_subject(operator: httpx.Client, stack: Stack, display="外部合作者 A"):
    private = Ed25519PrivateKey.generate()
    private_key = _b64(private.private_bytes_raw())
    public_key = _b64(private.public_key().public_bytes_raw())
    registered = _operator_admin(
        operator, "POST", "/v2/dsh/admin/subjects",
        json={"request_id": "req-dsh-reg-" + hashlib.sha256(public_key.encode()).hexdigest()[:12],
              "display_name": display, "public_key": public_key})
    assert registered.status_code == 201, registered.text
    body = registered.json()
    yield body, SignedClient(stack, body["subject_id"], body["key_id"], private_key)


def test_dsh_end_to_end_authorization(tmp_path):
    root = tmp_path
    with Stack(root) as stack:
        operator = httpx.Client(base_url=stack.operator, timeout=30)
        db = stack.dsh_db()

        # A01 -- operator token is mandatory and never trusted from loopback.
        no_token = operator.post("/v2/dsh/admin/subjects", json={
            "request_id": "req-dsh-reg-none", "display_name": "x",
            "public_key": _b64(b"0" * 32)})
        _error(no_token, 401, "DSH_OPERATOR_AUTH_REQUIRED")
        wrong = operator.post("/v2/dsh/admin/subjects",
                              headers={"X-DSH-Operator-Token": "wrong"}, json={
                                  "request_id": "req-dsh-reg-wrong", "display_name": "x",
                                  "public_key": _b64(b"0" * 32)})
        _error(wrong, 401, "DSH_OPERATOR_AUTH_REQUIRED")

        private = Ed25519PrivateKey.generate()
        private_key = _b64(private.private_bytes_raw())
        public_key = _b64(private.public_key().public_bytes_raw())
        register_body = {
            "request_id": "req-dsh-reg-0001",
            "display_name": "外部合作者 A",
            "public_key": public_key,
        }
        registered = _operator_admin(operator, "POST", "/v2/dsh/admin/subjects",
                                     json=register_body)
        assert registered.status_code == 201, registered.text
        registration = registered.json()
        assert registration["subject_id"].startswith("dsh_")
        assert len(registration["subject_id"]) == 36
        assert registration["key_id"].startswith("key_")
        assert len(registration["key_id"]) == 20
        replay_reg = _operator_admin(operator, "POST", "/v2/dsh/admin/subjects",
                                     json=register_body)
        assert replay_reg.status_code == 201
        assert replay_reg.json()["subject_id"] == registration["subject_id"]
        assert replay_reg.json()["replayed"] is True
        conflict_reg = _operator_admin(operator, "POST", "/v2/dsh/admin/subjects",
                                       json={**register_body, "display_name": "另一个"})
        _error(conflict_reg, 409, "DSH_REQUEST_ID_CONFLICT")
        subject_id = registration["subject_id"]
        key_id = registration["key_id"]
        dsh = SignedClient(stack, subject_id, key_id, private_key)

        # A03 -- registration alone grants nothing.
        _error(dsh.get(f"/v2/dsh/grants/grant_{'0' * 32}/activity",
                       grant_id="grant_" + "0" * 32,
                       params={"request_id": "req-dsh-no-grant-01"}),
               403, "DSH_GRANT_MISSING")

        # A26 -- an Activity needs a formal Person; DSH cannot create activities
        # or reach the admin surface from its own listener.
        only_dsh = operator.post("/v2/activities", json={
            "request_id": "req-activity-dshonly",
            "title": "只有 DSH",
            "participants": [{"actor_type": "DSH", "subject_id": subject_id}],
        })
        assert only_dsh.status_code == 422, only_dsh.text
        assert httpx.get(stack.dsh + "/v2/activities").status_code == 404
        assert httpx.post(stack.dsh + "/v2/dsh/admin/subjects",
                          json={"request_id": "x"}).status_code == 404

        activity_one = _create_activity(
            operator, subject_id, "req-activity-dsh-01", "共同整理旧照片")
        activity_two = _create_activity(
            operator, subject_id, "req-activity-dsh-02", "只有创建未开始",
            character="momo", resume=False)

        # A06 -- grant scope validation, and no grant row is written on failure.
        before_grants = db.execute("SELECT COUNT(*) FROM dsh_grants").fetchone()[0]
        bad_specs = [
            ({"scopes": ["MEMORY_READ"], "actions": []}, "DSH_SCOPE_NOT_GRANTABLE"),
            ({"scopes": ["PERSONA_WRITE"], "actions": []}, "DSH_SCOPE_NOT_GRANTABLE"),
            ({"scopes": ["*"], "actions": []}, "DSH_SCOPE_NOT_GRANTABLE"),
            ({"scopes": ["NOT_A_SCOPE"], "actions": []}, "DSH_SCOPE_UNKNOWN"),
            ({"scopes": ["ACTIVITY_PARTICIPATE"],
              "actions": ["ACTIVITY_LEAVE"]}, "DSH_SCOPE_DEPENDENCY"),
            ({"scopes": [], "actions": []}, "DSH_SCHEMA_INVALID"),
        ]
        for index, (spec, code) in enumerate(bad_specs):
            response = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
                "request_id": f"req-bad-grant-{index:02d}",
                "subject_id": subject_id,
                "activity_id": activity_one,
                **spec,
                "expires_at": (datetime.now(timezone.utc)
                               + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            })
            _error(response, 422, code)
        assert db.execute("SELECT COUNT(*) FROM dsh_grants").fetchone()[0] == before_grants

        # A02 -- identity/authorization fields are rejected and no fact appears.
        before_facts = db.execute("SELECT COUNT(*) FROM activity_facts").fetchone()[0]
        forbidden = dsh.post(
            f"/v2/dsh/grants/grant_{'1' * 32}/actions",
            grant_id="grant_" + "1" * 32,
            payload={"type": "ACTIVITY_SUBMIT_TURN",
                     "request_id": "req-dsh-forbidden-1",
                     "content": "hello", "subject_id": subject_id})
        _error(forbidden, 422, "DSH_IDENTITY_FIELD_FORBIDDEN")
        assert db.execute("SELECT COUNT(*) FROM activity_facts").fetchone()[0] == before_facts

        snapshot = _protected_counts(db)
        persona_hashes = _persona_hashes(root)

        # The primary grant.
        grant_one_resp = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-one-0001", subject_id, activity_one,
                          scopes=["ACTIVITY_READ_SELF", "ACTIVITY_READ_TRANSCRIPT",
                                  "ACTIVITY_PARTICIPATE"],
                          actions=["ACTIVITY_SUBMIT_TURN", "ACTIVITY_LEAVE"],
                          max_reads=200, max_actions=50),
        })
        assert grant_one_resp.status_code == 201, grant_one_resp.text
        grant_one = grant_one_resp.json()["grant_id"]
        grant_two_resp = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-two-0001", subject_id, activity_two,
                          scopes=["ACTIVITY_READ_SELF", "ACTIVITY_PARTICIPATE"],
                          actions=["ACTIVITY_SUBMIT_TURN", "ACTIVITY_LEAVE"],
                          max_actions=50),
        })
        assert grant_two_resp.status_code == 201, grant_two_resp.text
        grant_two = grant_two_resp.json()["grant_id"]

        # A07 -- a grant is bound to exactly one Activity.
        _error(dsh.get(f"/v2/dsh/grants/{grant_one}/activity", grant_id=grant_one,
                       params={"request_id": "req-dsh-cross-01",
                               "activity_id": activity_two}),
               403, "DSH_ACTIVITY_SCOPE_DENIED")
        own = dsh.get(f"/v2/dsh/grants/{grant_one}/activity", grant_id=grant_one,
                      params={"request_id": "req-dsh-own-01"})
        assert own.status_code == 200, own.text
        assert own.json()["id"] == activity_one

        # A08 -- scopes are enforced per operation; no side effect on denial.
        grant_read_only = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-readonly-1", subject_id, activity_one,
                          scopes=["ACTIVITY_READ_SELF"])}).json()["grant_id"]
        _error(dsh.get(f"/v2/dsh/grants/{grant_read_only}/transcript",
                       grant_id=grant_read_only,
                       params={"request_id": "req-dsh-noscope-01"}),
               403, "DSH_SCOPE_DENIED")
        _error(dsh.post(f"/v2/dsh/grants/{grant_read_only}/actions",
                        grant_id=grant_read_only,
                        payload={"type": "ACTIVITY_SUBMIT_TURN",
                                 "request_id": "req-dsh-noscope-02",
                                 "content": "no scope"}),
               403, "DSH_SCOPE_DENIED")
        assert db.execute("SELECT COUNT(*) FROM activity_facts").fetchone()[0] == before_facts

        # A09 -- max_reads=1, denied reads do not consume.
        grant_quota_read = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-quota-read", subject_id, activity_one,
                          scopes=["ACTIVITY_READ_SELF"], max_reads=1)}
        ).json()["grant_id"]
        denied = dsh.get(f"/v2/dsh/grants/{grant_quota_read}/transcript",
                         grant_id=grant_quota_read,
                         params={"request_id": "req-dsh-q-read-deny"})
        _error(denied, 403, "DSH_SCOPE_DENIED")
        first = dsh.get(f"/v2/dsh/grants/{grant_quota_read}/activity",
                        grant_id=grant_quota_read,
                        params={"request_id": "req-dsh-q-read-01"})
        assert first.status_code == 200, first.text
        second = dsh.get(f"/v2/dsh/grants/{grant_quota_read}/activity",
                         grant_id=grant_quota_read,
                         params={"request_id": "req-dsh-q-read-02"})
        _error(second, 403, "DSH_QUOTA_EXHAUSTED")

        # A04 -- the signature binds path; a nonce is one-shot.
        prepared = dsh.prepare("GET", f"/v2/dsh/grants/{grant_one}/activity",
                               grant_id=grant_one,
                               params={"request_id": "req-dsh-sign-01"})
        moved = {**prepared, "path": "/v2/dsh/self"}
        _error(dsh.send(moved), 401, "DSH_AUTHENTICATION_FAILED")
        valid = dsh.send(prepared)
        assert valid.status_code == 200, valid.text
        _error(dsh.send(prepared), 401, "DSH_NONCE_REPLAY")

        # A11/A12/A19 -- one typed turn, replay and canonical conflict.
        turn_payload = {"type": "ACTIVITY_SUBMIT_TURN",
                        "request_id": "req-dsh-turn-0001",
                        "content": "大家好，我是外部参与者"}
        turn = dsh.post(f"/v2/dsh/grants/{grant_one}/actions",
                        grant_id=grant_one, payload=turn_payload)
        assert turn.status_code == 200, turn.text
        fact = turn.json()["fact"]
        assert fact["experience_scope"] == {
            "schema_version": 1, "reality": "SIMULATED", "activity_id": activity_one}
        assert fact["authorization"]["grant_id"] == grant_one
        assert fact["authorization"]["status_at_commit"] == "ACTIVE"
        turn_replay = dsh.post(f"/v2/dsh/grants/{grant_one}/actions",
                               grant_id=grant_one, payload=turn_payload)
        assert turn_replay.status_code == 200 and turn_replay.json()["replayed"] is True
        assert db.execute("SELECT COUNT(*) FROM activity_facts").fetchone()[0] == before_facts + 1
        assert db.execute(
            "SELECT COUNT(*) FROM dsh_request_receipts WHERE grant_id=? AND request_id=?",
            (grant_one, turn_payload["request_id"])).fetchone()[0] == 1
        conflict = dsh.post(f"/v2/dsh/grants/{grant_one}/actions",
                            grant_id=grant_one,
                            payload={**turn_payload, "content": "换了内容"})
        _error(conflict, 409, "DSH_REQUEST_ID_CONFLICT")
        assert db.execute("SELECT COUNT(*) FROM activity_facts").fetchone()[0] == before_facts + 1
        assert db.execute("SELECT actions_used FROM dsh_grants WHERE grant_id=?",
                          (grant_one,)).fetchone()[0] == 1

        # A19 -- SIMULATED facts are excluded by the real-experience SQL and do
        # not touch Memory/Event/Mental State/Persona/Space/Group/Direct rows.
        real_count = db.execute(
            "SELECT COUNT(*) FROM (SELECT json_object('experience_scope', "
            "json(experience_scope_json)) AS metadata_json FROM activity_facts) "
            "WHERE " + _real_scope_sql("metadata_json")).fetchone()[0]
        assert real_count == 0
        assert _protected_counts(db) == snapshot
        assert _persona_hashes(root) == persona_hashes

        # Transcript reads the fact and enforces the stored scope.
        transcript = dsh.get(f"/v2/dsh/grants/{grant_one}/transcript",
                             grant_id=grant_one,
                             params={"request_id": "req-dsh-transcript-1"})
        assert transcript.status_code == 200, transcript.text
        assert [item["fact_id"] for item in transcript.json()["facts"]] == [fact["fact_id"]]
        assert transcript.json()["facts"][0]["actor"]["subject_id"] == subject_id

        # A20 -- lifecycle combinations.
        _error(dsh.post(f"/v2/dsh/grants/{grant_two}/actions", grant_id=grant_two,
                        payload={"type": "ACTIVITY_SUBMIT_TURN",
                                 "request_id": "req-dsh-created-01",
                                 "content": "还没开始"}),
               409, "ACTIVITY_STATE_CONFLICT")
        left = dsh.post(f"/v2/dsh/grants/{grant_two}/actions", grant_id=grant_two,
                        payload={"type": "ACTIVITY_LEAVE",
                                 "request_id": "req-dsh-leave-0001"})
        assert left.status_code == 200, left.text
        assert left.json()["status"] == "LEFT"
        assert operator.get(f"/v2/activities/{activity_two}").json()["status"] == "CREATED"
        left_again = dsh.post(f"/v2/dsh/grants/{grant_two}/actions", grant_id=grant_two,
                              payload={"type": "ACTIVITY_LEAVE",
                                       "request_id": "req-dsh-leave-0002"})
        assert left_again.status_code == 200 and left_again.json()["status"] == "LEFT"
        resumed = operator.post(f"/v2/activities/{activity_two}/resume",
                                json={"request_id": "req-activity-two-resume",
                                      "expected_version": 1})
        assert resumed.status_code == 200, resumed.text
        _error(dsh.post(f"/v2/dsh/grants/{grant_two}/actions", grant_id=grant_two,
                        payload={"type": "ACTIVITY_SUBMIT_TURN",
                                 "request_id": "req-dsh-left-turn-1",
                                 "content": "离开后"}),
               409, "DSH_PARTICIPANT_LEFT")
        ended = operator.post(f"/v2/activities/{activity_two}/end",
                              json={"request_id": "req-activity-two-end",
                                    "expected_version": 2})
        assert ended.status_code == 200, ended.text
        _error(dsh.post(f"/v2/dsh/grants/{grant_two}/actions", grant_id=grant_two,
                        payload={"type": "ACTIVITY_LEAVE",
                                 "request_id": "req-dsh-leave-ended"}),
               409, "ACTIVITY_STATE_CONFLICT")

        # A10 -- a failed write rolls back the reservation and leaves no fact.
        grant_write = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-write-0001", subject_id, activity_one,
                          scopes=["ACTIVITY_READ_SELF", "ACTIVITY_PARTICIPATE"],
                          actions=["ACTIVITY_SUBMIT_TURN", "ACTIVITY_LEAVE"],
                          max_actions=1)}).json()["grant_id"]
        facts_before = db.execute("SELECT COUNT(*) FROM activity_facts").fetchone()[0]
        arm = httpx.post(stack.dsh + "/__e2e/dsh/fail-next-write")
        assert arm.status_code == 200
        failed = dsh.post(f"/v2/dsh/grants/{grant_write}/actions", grant_id=grant_write,
                          payload={"type": "ACTIVITY_SUBMIT_TURN",
                                   "request_id": "req-dsh-writefail-1",
                                   "content": "会失败"})
        _error(failed, 500, "DSH_WRITE_FAILED")
        assert db.execute("SELECT COUNT(*) FROM activity_facts").fetchone()[0] == facts_before
        assert db.execute("SELECT actions_used FROM dsh_grants WHERE grant_id=?",
                          (grant_write,)).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM dsh_audit_events WHERE event_type='ACTION_FAIL' "
            "AND grant_id=?", (grant_write,)).fetchone()[0] == 1
        succeeded = dsh.post(f"/v2/dsh/grants/{grant_write}/actions", grant_id=grant_write,
                             payload={"type": "ACTIVITY_SUBMIT_TURN",
                                      "request_id": "req-dsh-writeok-01",
                                      "content": "成功"})
        assert succeeded.status_code == 200, succeeded.text
        exhausted = dsh.post(f"/v2/dsh/grants/{grant_write}/actions", grant_id=grant_write,
                             payload={"type": "ACTIVITY_SUBMIT_TURN",
                                      "request_id": "req-dsh-writeok-02",
                                      "content": "第二次"})
        _error(exhausted, 403, "DSH_QUOTA_EXHAUSTED")

        # A05 -- keys are independent; two ACTIVE keys max; revoking a key does
        # not revoke grants, revoking the subject does.
        second_private = Ed25519PrivateKey.generate()
        second_key_resp = _operator_admin(operator, "POST",
                                          f"/v2/dsh/admin/subjects/{subject_id}/keys",
                                          json={"request_id": "req-dsh-key-0002",
                                                "public_key": _b64(
                                                    second_private.public_key().public_bytes_raw())})
        assert second_key_resp.status_code == 201, second_key_resp.text
        second_key_id = second_key_resp.json()["key_id"]
        third_key = _operator_admin(operator, "POST",
                                    f"/v2/dsh/admin/subjects/{subject_id}/keys",
                                    json={"request_id": "req-dsh-key-0003",
                                          "public_key": _b64(Ed25519PrivateKey.generate()
                                                             .public_key().public_bytes_raw())})
        _error(third_key, 409, "DSH_KEY_LIMIT")
        second_client = SignedClient(stack, subject_id, second_key_id,
                                     _b64(second_private.private_bytes_raw()))
        assert second_client.get("/v2/dsh/self", params={
            "request_id": "req-dsh-self-key2"}).status_code == 200
        revoked_key = _operator_admin(
            operator, "POST",
            f"/v2/dsh/admin/subjects/{subject_id}/keys/{key_id}/revoke",
            json={"request_id": "req-dsh-revoke-key1"})
        assert revoked_key.status_code == 200, revoked_key.text
        _error(dsh.get("/v2/dsh/self", params={"request_id": "req-dsh-revoked-key"}),
               401, "DSH_KEY_REVOKED")
        assert second_client.get("/v2/dsh/self", params={
            "request_id": "req-dsh-self-key2b"}).status_code == 200
        assert db.execute("SELECT status FROM dsh_grants WHERE grant_id=?",
                          (grant_one,)).fetchone()[0] == "ACTIVE"

        # A16/A18 -- revocation is immediate; committed facts are relabelled.
        fact_before = db.execute(
            "SELECT authorization_json FROM activity_facts WHERE activity_id=? ORDER BY seq LIMIT 1",
            (activity_one,)).fetchone()[0]
        revoked_grant = _operator_admin(
            operator, "POST", f"/v2/dsh/admin/grants/{grant_one}/revoke",
            json={"request_id": "req-dsh-revoke-grant1"})
        assert revoked_grant.status_code == 200, revoked_grant.text
        _error(second_client.get(f"/v2/dsh/grants/{grant_one}/activity",
                                 grant_id=grant_one,
                                 params={"request_id": "req-dsh-after-revoke"}),
               403, "DSH_GRANT_REVOKED")
        facts_admin = _operator_admin(
            operator, "GET", f"/v2/dsh/admin/grants/{grant_one}/facts")
        assert facts_admin.status_code == 200, facts_admin.text
        projected = facts_admin.json()["facts"]
        assert projected and all(item["authorization_status"] == "REVOKED_AFTER_COMMIT"
                                 for item in projected)
        fact_after = db.execute(
            "SELECT authorization_json FROM activity_facts WHERE activity_id=? ORDER BY seq LIMIT 1",
            (activity_one,)).fetchone()[0]
        assert fact_after == fact_before

        # A13/A14/A15 -- expiry uses server admission time only.
        expiring = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body(
                "req-grant-expire-0001", subject_id, activity_one,
                scopes=["ACTIVITY_READ_SELF"],
                expires_at=(datetime.now(timezone.utc)
                            + timedelta(seconds=1.2)).isoformat().replace("+00:00", "Z"))}
        ).json()["grant_id"]
        time.sleep(1.4)
        _error(second_client.get(f"/v2/dsh/grants/{expiring}/activity",
                                 grant_id=expiring,
                                 params={"request_id": "req-dsh-expired-01"}),
               403, "DSH_GRANT_EXPIRED")

        stale_prepared = second_client.prepare(
            "GET", f"/v2/dsh/grants/{expiring}/activity", grant_id=expiring,
            params={"request_id": "req-dsh-late-sign-1"})
        time.sleep(1.2)
        _error(second_client.send(stale_prepared), 403, "DSH_GRANT_EXPIRED")

        late_grant = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body(
                "req-grant-late-lock-1", subject_id, activity_one,
                scopes=["ACTIVITY_READ_SELF", "ACTIVITY_PARTICIPATE"],
                actions=["ACTIVITY_SUBMIT_TURN", "ACTIVITY_LEAVE"],
                max_actions=10,
                expires_at=(datetime.now(timezone.utc)
                            + timedelta(seconds=1.5)).isoformat().replace("+00:00", "Z"))}
        ).json()["grant_id"]
        blocker = sqlite3.connect(root / "activity.db", timeout=15)
        blocker.execute("BEGIN IMMEDIATE")
        late_result = {}

        def late_action():
            late_result["response"] = second_client.post(
                f"/v2/dsh/grants/{late_grant}/actions", grant_id=late_grant,
                payload={"type": "ACTIVITY_SUBMIT_TURN",
                         "request_id": "req-dsh-admission-late",
                         "content": "排队后过期"})

        thread = threading.Thread(target=late_action)
        thread.start()
        time.sleep(2.0)
        blocker.rollback()
        blocker.close()
        thread.join(timeout=20)
        _error(late_result["response"], 403, "DSH_GRANT_EXPIRED")
        assert db.execute("SELECT actions_used FROM dsh_grants WHERE grant_id=?",
                          (late_grant,)).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM activity_facts "
            "WHERE json_extract(authorization_json,'$.grant_id')=?",
            (late_grant,)).fetchone()[0] == 0

        # A15 -- admitted before expiry, committed after, is valid. The guard
        # sleeps while the transaction is open, so commit happens after expiry.
        slow_grant_body = _grant_body(
            "req-grant-slow-commit", subject_id, activity_one,
            scopes=["ACTIVITY_READ_SELF", "ACTIVITY_PARTICIPATE"],
            actions=["ACTIVITY_SUBMIT_TURN"], max_actions=5,
            expires_at=(datetime.now(timezone.utc)
                        + timedelta(seconds=1.2)).isoformat().replace("+00:00", "Z"))
        slow_grant = _operator_admin(operator, "POST", "/v2/dsh/admin/grants",
                                     json=slow_grant_body).json()["grant_id"]
        httpx.post(stack.dsh + "/__e2e/dsh/delay-next-write", params={"seconds": 1.6})
        slow = second_client.post(f"/v2/dsh/grants/{slow_grant}/actions",
                                  grant_id=slow_grant,
                                  payload={"type": "ACTIVITY_SUBMIT_TURN",
                                           "request_id": "req-dsh-slow-commit",
                                           "content": "提交晚于过期"})
        assert slow.status_code == 200, slow.text
        admitted = datetime.fromisoformat(slow.json()["admitted_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(slow.json()["expires_at"].replace("Z", "+00:00"))
        assert admitted < expires
        assert datetime.now(timezone.utc) > expires
        audit_row = db.execute(
            "SELECT detail_json FROM dsh_audit_events WHERE request_id=? "
            "AND event_type='ACTION_COMMIT'", ("req-dsh-slow-commit",)).fetchone()
        detail = json.loads(audit_row[0])
        assert detail["admitted_at"] == slow.json()["admitted_at"]
        assert detail["expires_at"] == slow.json()["expires_at"]

        # A25 -- concurrent identical requests create exactly one fact.
        concurrent_grant = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-concurrent", subject_id, activity_one,
                          scopes=["ACTIVITY_READ_SELF", "ACTIVITY_PARTICIPATE"],
                          actions=["ACTIVITY_SUBMIT_TURN"], max_actions=10)}
        ).json()["grant_id"]
        facts_before = db.execute("SELECT COUNT(*) FROM activity_facts").fetchone()[0]
        payload = {"type": "ACTIVITY_SUBMIT_TURN",
                   "request_id": "req-dsh-concurrent-1", "content": "并发同请求"}
        results = []

        def fire():
            client = httpx.Client(timeout=30)
            try:
                results.append(second_client.post(
                    f"/v2/dsh/grants/{concurrent_grant}/actions",
                    grant_id=concurrent_grant, payload=payload))
            finally:
                client.close()

        threads = [threading.Thread(target=fire) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert len(results) == 2 and all(item.status_code == 200 for item in results), results
        assert sorted(item.json()["replayed"] for item in results) == [False, True]
        assert db.execute("SELECT COUNT(*) FROM activity_facts").fetchone()[0] == facts_before + 1
        assert db.execute("SELECT actions_used FROM dsh_grants WHERE grant_id=?",
                          (concurrent_grant,)).fetchone()[0] == 1

        # A25 -- revoke/action contention yields either commit-then-revoke or
        # revoke-then-reject, never a half state.
        race_grant = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-race-0001", subject_id, activity_one,
                          scopes=["ACTIVITY_READ_SELF", "ACTIVITY_PARTICIPATE"],
                          actions=["ACTIVITY_SUBMIT_TURN"], max_actions=10)}
        ).json()["grant_id"]
        race_result = {}

        def race_action():
            race_result["action"] = dsh_race_client(stack, subject_id, second_key_id,
                                                    _b64(second_private.private_bytes_raw()),
                                                    race_grant)

        thread = threading.Thread(target=race_action)
        thread.start()
        revoke = _operator_admin(operator, "POST",
                                 f"/v2/dsh/admin/grants/{race_grant}/revoke",
                                 json={"request_id": "req-dsh-race-revoke"})
        thread.join(timeout=30)
        assert revoke.status_code == 200, revoke.text
        response = race_result["action"]
        if response.status_code == 200:
            assert db.execute(
                "SELECT COUNT(*) FROM activity_facts WHERE activity_id=? AND authorization_json LIKE ?",
                (activity_one, f"%{race_grant}%")).fetchone()[0] == 1
            facts_race = _operator_admin(
                operator, "GET", f"/v2/dsh/admin/grants/{race_grant}/facts").json()["facts"]
            assert all(item["authorization_status"] == "REVOKED_AFTER_COMMIT"
                       for item in facts_race)
        else:
            assert response.status_code == 403, response.text
            assert db.execute(
                "SELECT COUNT(*) FROM activity_facts WHERE authorization_json LIKE ?",
                (f"%{race_grant}%",)).fetchone()[0] == 0

        # A06/A22 -- non-grantable read surfaces and unlisted Space posts.
        for index, scope in enumerate(("MEMORY_READ", "PERSONA_WRITE", "DIRECT_READ",
                                       "GROUP_READ", "SQL", "MODEL_INVOKE")):
            denied_grant = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
                **_grant_body(f"req-grant-denied-{index}", subject_id, activity_one,
                              scopes=[scope])})
            _error(denied_grant, 422, "DSH_SCOPE_NOT_GRANTABLE")
        post_one = operator.post("/v1/space/posts",
                                 json={"character_id": "rin", "content": "公开的 Space 帖"})
        assert post_one.status_code == 200, post_one.text
        post_one_id = post_one.json()["post"]["id"]
        post_two = operator.post("/v1/space/posts",
                                 json={"character_id": "rin", "content": "未授权的 Space 帖"})
        assert post_two.status_code == 200, post_two.text
        post_two_id = post_two.json()["post"]["id"]
        space_counts_before = _space_counts(db)
        space_grant = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-space-0001", subject_id, activity_one,
                          scopes=["SPACE_POST_READ"],
                          public_facts=[{"kind": "SPACE_POST", "id": post_one_id}])}
        ).json()["grant_id"]
        _error(second_client.get(
            f"/v2/dsh/grants/{space_grant}/facts/space_post/{post_two_id}",
            grant_id=space_grant,
            params={"request_id": "req-dsh-space-denied"}),
            403, "DSH_SCOPE_DENIED")
        space_post = second_client.get(
            f"/v2/dsh/grants/{space_grant}/facts/space_post/{post_one_id}",
            grant_id=space_grant, params={"request_id": "req-dsh-space-ok"})
        assert space_post.status_code == 200, space_post.text
        body = space_post.json()
        assert body["author"]["id"] == "rin"
        assert set(body) >= {"id", "character_id", "author", "content", "created_at",
                             "visibility", "media_items"}
        assert "comments" not in body and "likes" not in body
        assert "source_event_id" not in body
        assert body["scope_status"] in {"LEGACY_REAL", "SIMULATED", "REAL"}
        assert _space_counts(db) == space_counts_before
        for path in ("/v1/characters/rin/memories", "/v1/group/history",
                     "/v1/space/posts", "/v1/diagnostics", "/v1/chat/history-page"):
            assert httpx.get(stack.dsh + path).status_code == 404, path

        # A17 -- subject revocation atomically revokes every grant.
        subject_two_private = Ed25519PrivateKey.generate()
        subject_two = _operator_admin(operator, "POST", "/v2/dsh/admin/subjects", json={
            "request_id": "req-dsh-reg-subject2", "display_name": "外部合作者 B",
            "public_key": _b64(subject_two_private.public_key().public_bytes_raw())}
        ).json()
        subject_two_id = subject_two["subject_id"]
        subject_two_client = SignedClient(
            stack, subject_two_id, subject_two["key_id"],
            _b64(subject_two_private.private_bytes_raw()))
        activity_three = _create_activity(operator, subject_two_id,
                                          "req-activity-dsh-03", "第三个活动")
        grant_three_a = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-three-a", subject_two_id, activity_three,
                          scopes=["ACTIVITY_READ_SELF"])}).json()["grant_id"]
        grant_three_b = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-three-b", subject_two_id, activity_three,
                          scopes=["ACTIVITY_READ_SELF"])}).json()["grant_id"]
        revoked_subject = _operator_admin(
            operator, "POST", f"/v2/dsh/admin/subjects/{subject_two_id}/revoke",
            json={"request_id": "req-dsh-revoke-subject2"})
        assert revoked_subject.status_code == 200, revoked_subject.text
        statuses = {row[0] for row in db.execute(
            "SELECT status FROM dsh_grants WHERE subject_id=?", (subject_two_id,))}
        assert statuses == {"REVOKED"}
        assert {row[0] for row in db.execute(
            "SELECT grant_id FROM dsh_grants WHERE grant_id IN (?,?)",
            (grant_three_a, grant_three_b))} == {grant_three_a, grant_three_b}
        _error(subject_two_client.get(
            f"/v2/dsh/grants/{grant_three_a}/activity", grant_id=grant_three_a,
            params={"request_id": "req-dsh-revoked-subject"}),
            403, "DSH_SUBJECT_REVOKED")

        # A real external client script performs one signed participation turn.
        script_grant = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-grant-script-0001", subject_id, activity_one,
                          scopes=["ACTIVITY_READ_SELF", "ACTIVITY_PARTICIPATE"],
                          actions=["ACTIVITY_SUBMIT_TURN"])}
        ).json()["grant_id"]
        script_env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
                      "PYTHONIOENCODING": "utf-8",
                      "DSH_PRIVATE_KEY": _b64(second_private.private_bytes_raw())}
        script = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dsh_client.py"), "submit-turn",
             "--origin", stack.dsh, "--subject", subject_id,
             "--key-id", second_key_id, "--grant", script_grant,
             "--request-id", "req-script-turn-01",
             "--content", "脚本客户端参与"],
            cwd=ROOT, env=script_env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        assert script.returncode == 0, script.stdout + script.stderr
        script_body = json.loads(script.stdout)
        assert script_body["status"] == 200
        assert script_body["body"]["fact"]["authorization"]["grant_id"] == script_grant

        # A23 -- one terminal audit per request, verifiable chain, tamper located.
        terminal = db.execute(
            "SELECT event_type FROM dsh_audit_events WHERE request_id=? "
            "AND event_type!='ACTION_RESERVE'", ("req-dsh-writeok-01",)).fetchall()
        assert [row[0] for row in terminal] == ["ACTION_COMMIT"]
        verified = _operator_admin(operator, "GET", "/v2/dsh/admin/audit/verify")
        assert verified.status_code == 200, verified.text
        assert verified.json()["ok"] is True
        last_id = db.execute("SELECT MAX(id) FROM dsh_audit_events").fetchone()[0]
        db.execute("UPDATE dsh_audit_events SET detail_json='{\"tampered\":true}' WHERE id=?",
                   (last_id,))
        db.commit()
        broken = _operator_admin(operator, "GET", "/v2/dsh/admin/audit/verify")
        assert broken.status_code == 200
        assert broken.json()["ok"] is False
        assert broken.json()["first_broken_id"] == last_id

        # A17 (key revoke does not revoke grants) was asserted above; subject
        # revoke is atomic. A27 -- operator listener does not accept DSH
        # authentication and the DSH listener exposes no Main API routes.
        operator_self = dsh.prepare("GET", "/v2/dsh/self",
                                    params={"request_id": "req-dsh-operator-self"})
        assert httpx.get(stack.operator + operator_self["path"],
                         headers=operator_self["headers"]).status_code == 404
        admin_with_dsh_headers = httpx.get(
            stack.operator + "/v2/dsh/admin/audit",
            headers={k: v for k, v in operator_self["headers"].items()})
        assert admin_with_dsh_headers.status_code == 401
        assert admin_with_dsh_headers.json()["detail"]["code"] == "DSH_OPERATOR_AUTH_REQUIRED"
        for path in ("/v1/chat/history-page", "/v2/activities",
                     "/openapi.json", "/docs"):
            assert httpx.get(stack.dsh + path).status_code == 404, path

        # A21 -- no Person fact tables changed after the entire business run.
        assert _protected_counts(db) == snapshot
        assert _persona_hashes(root) == persona_hashes
        db.close()
        operator.close()


def dsh_race_client(stack, subject_id, key_id, private_key, grant_id):
    client = SignedClient(stack, subject_id, key_id, private_key)
    return client.post(f"/v2/dsh/grants/{grant_id}/actions", grant_id=grant_id,
                       payload={"type": "ACTIVITY_SUBMIT_TURN",
                                "request_id": "req-dsh-race-action",
                                "content": "与撤销竞争"})


def _real_scope_sql(column: str) -> str:
    from character_memory.domain.experience_scope import real_experience_sql
    return real_experience_sql(column)


def _protected_counts(db) -> dict:
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    wanted = ["memories", "events", "mental_states", "intents", "conversation_events"]
    counts = {}
    for table in wanted:
        if table in tables:
            counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


def _space_counts(db) -> dict:
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    wanted = ["space_posts", "space_comments", "space_reactions", "space_notifications"]
    return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in wanted if table in tables}


def _persona_hashes(root: Path) -> dict:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "personas").rglob("persona.yaml"))
    }


def test_dsh_state_and_audit_survive_restart(tmp_path):
    root = tmp_path
    with Stack(root) as stack:
        operator = httpx.Client(base_url=stack.operator, timeout=30)
        private = Ed25519PrivateKey.generate()
        private_key = _b64(private.private_bytes_raw())
        registered = _operator_admin(operator, "POST", "/v2/dsh/admin/subjects", json={
            "request_id": "req-restart-register",
            "display_name": "重启主体",
            "public_key": _b64(private.public_key().public_bytes_raw())}).json()
        subject_id = registered["subject_id"]
        dsh = SignedClient(stack, subject_id, registered["key_id"], private_key)
        activity = _create_activity(operator, subject_id, "req-restart-activity-1",
                                    "重启后仍可读")
        grant = _operator_admin(operator, "POST", "/v2/dsh/admin/grants", json={
            **_grant_body("req-restart-grant-1", subject_id, activity,
                          scopes=["ACTIVITY_READ_SELF", "ACTIVITY_PARTICIPATE"],
                          actions=["ACTIVITY_SUBMIT_TURN"], max_reads=1, max_actions=5)}
        ).json()["grant_id"]
        turn_payload = {"type": "ACTIVITY_SUBMIT_TURN",
                        "request_id": "req-restart-turn-1", "content": "重启前"}
        first = dsh.post(f"/v2/dsh/grants/{grant}/actions",
                         grant_id=grant, payload=turn_payload)
        assert first.status_code == 200, first.text
        read = dsh.get(f"/v2/dsh/grants/{grant}/activity", grant_id=grant,
                       params={"request_id": "req-restart-read-1"})
        assert read.status_code == 200
        operator.close()
        first_pid = httpx.get(stack.dsh + "/__e2e/dsh/runtime").json()["pid"]

    with Stack(root) as stack:
        operator = httpx.Client(base_url=stack.operator, timeout=30)
        dsh = SignedClient(stack, subject_id, registered["key_id"], private_key)
        assert httpx.get(stack.dsh + "/__e2e/dsh/runtime").json()["pid"] != first_pid
        replay = dsh.post(f"/v2/dsh/grants/{grant}/actions",
                          grant_id=grant, payload=turn_payload)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        # The read quota was consumed before the restart; replaying the same
        # read returns the original result without consuming again.
        read_replay = dsh.get(f"/v2/dsh/grants/{grant}/activity", grant_id=grant,
                              params={"request_id": "req-restart-read-1"})
        assert read_replay.status_code == 200 and read_replay.json()["replayed"] is True
        exhausted = dsh.get(f"/v2/dsh/grants/{grant}/activity", grant_id=grant,
                            params={"request_id": "req-restart-read-2"})
        _error(exhausted, 403, "DSH_QUOTA_EXHAUSTED")
        verified = _operator_admin(operator, "GET", "/v2/dsh/admin/audit/verify")
        assert verified.status_code == 200 and verified.json()["ok"] is True
        runtime = httpx.get(stack.dsh + "/__e2e/dsh/runtime").json()
        assert runtime["model_loaded"] is False
        operator.close()


def test_production_composition_mounts_dsh_admin_only_on_operator(tmp_path):
    """server.py owns the admin surface; dsh_server.py owns the business one."""
    from fastapi.testclient import TestClient

    config = _config(tmp_path)
    # dsh_server.py exposes a module-level app for uvicorn; point it at this
    # temporary database before importing it so no real runtime DB is touched.
    previous = os.environ.get("CHARACTER_MEMORY_CONFIG")
    os.environ["CHARACTER_MEMORY_CONFIG"] = str(config)
    try:
        from character_memory.dsh_server import create_dsh_app
        from character_memory.server import create_server_app
    finally:
        if previous is None:
            os.environ.pop("CHARACTER_MEMORY_CONFIG", None)
        else:
            os.environ["CHARACTER_MEMORY_CONFIG"] = previous
    operator_app = create_server_app(str(config))
    with TestClient(operator_app) as client:
        no_token = client.post("/v2/dsh/admin/subjects", json={
            "request_id": "req-prod-admin-01", "display_name": "x",
            "public_key": _b64(b"1" * 32)})
        assert no_token.status_code == 401
        assert no_token.json()["detail"]["code"] == "DSH_OPERATOR_AUTH_REQUIRED"
        # The business surface is not mounted on the operator app.
        assert client.get("/v2/dsh/self?request_id=req-prod-self").status_code == 404
        assert client.get("/v2/activities").status_code == 200

    dsh_app = create_dsh_app(str(config))
    with TestClient(dsh_app) as client:
        assert client.get("/health").json()["service"] == "dsh"
        for path in ("/openapi.json", "/docs", "/v2/activities",
                     "/v2/dsh/admin/subjects", "/v1/chat/history-page"):
            assert client.get(path).status_code == 404, path
        # No signature: the business surface still exists and requires auth.
        assert client.get("/v2/dsh/self?request_id=req-prod-self").status_code == 401


def test_grant_expiry_boundary_is_server_derived():
    """A13: ``server_now == expires_at`` is already expired."""
    from character_memory.dsh.store import DshStore, iso_utc, parse_utc

    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    grant = {"status": "ACTIVE", "expires_at": iso_utc(now)}
    subject = {"status": "ACTIVE"}
    assert DshStore.effective_grant_status(grant, subject, now) == "EXPIRED"
    assert DshStore.effective_grant_status(
        grant, subject, now - timedelta(microseconds=1)) == "ACTIVE"
    assert parse_utc(grant["expires_at"]) == now
