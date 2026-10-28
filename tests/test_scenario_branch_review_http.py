"""P12 real-HTTP acceptance: isolated branches, durable review, explicit adoption.

A temp SQLite database and temp persona root back real uvicorn processes. The
deterministic model records one JSON line per call, so zero-call reads, failed
generations and retries are observed counts rather than assumptions. The same
database is reopened by a second process to prove branch/review/adoption restore.
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
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
PERSONAS = ("rin", "momo")


def _write_personas(root: Path) -> None:
    for name in PERSONAS:
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "persona.yaml").write_text(
            f"id: {name}\nname: {name.title()}\nidentity: e2e scenario character\n",
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
        "CHARACTER_MEMORY_SCENARIO_DB": str(root / "scenario.db"),
        "CHARACTER_MEMORY_SCENARIO_MEDIA": str(root / "media"),
        "CHARACTER_MEMORY_SCENARIO_PERSONAS": str(root / "personas"),
        "SCENARIO_MODEL_CALL_LOG": str(root / "model-calls.jsonl"),
    }
    options = {}
    tree = None
    if os.name == "nt":
        from character_memory.runtime.process_tree import WindowsProcessTree

        tree = WindowsProcessTree()
        options["creationflags"] = 0x00000004 | 0x08000000
    with (root / f"api-{port}.log").open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "e2e_scenario_app:app",
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
                raise RuntimeError("scenario HTTP readiness timeout")
            with httpx.Client(base_url=origin, timeout=30) as client:
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


def create_body(request_id="req-scenario-p12-01"):
    return {
        "request_id": request_id,
        "title": "和家里说想换工作",
        "situation": "我正在考虑辞掉现在的工作，换一份收入不稳定但更喜欢的工作，今晚要和妈妈、姐姐吃饭。",
        "goal": "把计划说清楚，也听听她们的担心，不吵起来。",
        "concern": "怕一开口就变成辩解，也怕她们觉得我不负责任。",
        "participants": [
            {"actor_type": "CHARACTER", "character_id": "rin"},
            {"actor_type": "SIMULATED_CHARACTER", "local_id": "mother", "display_name": "妈妈"},
            {"actor_type": "SIMULATED_CHARACTER", "local_id": "sister", "display_name": "姐姐"},
        ],
        "descriptions": {
            "sim:mother": "担心我辞职后没有稳定收入，习惯先问最坏的情况",
            "sim:sister": "务实，愿意帮我想步骤，但不喜欢听空话",
        },
    }


def calls(root: Path) -> list[dict]:
    log = root / "model-calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def kinds(root: Path, kind: str) -> int:
    return sum(1 for item in calls(root) if item["kind"] == kind)


def test_branch_review_and_shared_memory_over_real_http(tmp_path):
    pids = []
    with server(tmp_path) as client:
        pids.append(client.get("/__e2e/audit").json()["pid"])
        created = client.post("/v2/scenarios", json=create_body())
        assert created.status_code == 201, created.text
        activity_id = created.json()["activity"]["id"]
        assert created.json()["activity"]["status"] == "CREATED"
        resumed = client.post(f"/v2/activities/{activity_id}/resume",
                              json={"request_id": "req-start-p12-01", "expected_version": 1})
        assert resumed.status_code == 200, resumed.text

        first = client.post(f"/v2/scenarios/{activity_id}/messages",
                            json={"request_id": "req-turn-p12-01",
                                  "message": "我今晚想先讲我的计划，希望你们先听我说完。"})
        assert first.status_code == 200, first.text
        first_body = first.json()
        branch_point = first_body["events"][-1]["id"]
        main_events = client.get(f"/v2/scenarios/{activity_id}").json()["scenario"]["events"]
        assert len(main_events) == 6

        # --- isolated branches from the same branch point -----------------
        branch_a = client.post(f"/v2/scenarios/{activity_id}/branches", json={
            "request_id": "req-branch-a-001",
            "after_event_id": branch_point,
            "message": "如果我说不出口，只想先安静一会儿呢？",
        })
        assert branch_a.status_code == 201, branch_a.text
        branch_a_body = branch_a.json()
        assert branch_a_body["status"] == "READY"
        assert branch_a_body["consequence"]
        assert branch_a_body["experience_scope"]["branch_id"] == branch_a_body["branch_id"]
        branch_a_id = branch_a_body["branch_id"]

        branch_b = client.post(f"/v2/scenarios/{activity_id}/branches", json={
            "request_id": "req-branch-b-001",
            "after_event_id": branch_point,
            "message": "如果我直接拒绝呢？",
        })
        assert branch_b.status_code == 201, branch_b.text
        branch_b_id = branch_b.json()["branch_id"]
        assert branch_b_id != branch_a_id

        # The main transcript did not absorb either branch's turn.
        main = client.get(f"/v2/scenarios/{activity_id}").json()["scenario"]
        assert len(main["events"]) == 6
        assert all(item["branch_id"] is None for item in main["events"])
        assert {item["branch_id"] for item in main["branches"]} == {branch_a_id, branch_b_id}
        left = client.get(f"/v2/scenarios/{activity_id}/branches/{branch_a_id}").json()
        right = client.get(f"/v2/scenarios/{activity_id}/branches/{branch_b_id}").json()
        assert left["turn_id"] != right["turn_id"]
        assert all(item["branch_id"] == branch_a_id for item in left["events"])
        assert all(item["branch_id"] == branch_b_id for item in right["events"])
        assert {item["content"] for item in left["events"]
                if item["event_type"] == "USER_MESSAGE"} == {
            "如果我说不出口，只想先安静一会儿呢？"}
        assert {item["content"] for item in right["events"]
                if item["event_type"] == "USER_MESSAGE"} == {"如果我直接拒绝呢？"}
        for event in left["events"] + right["events"]:
            assert event["experience_scope"]["reality"] == "SIMULATED"
        assert kinds(tmp_path, "branch") == 2

        # --- a failed branch is durable, replayed, then explicitly retried
        client.post("/__e2e/faults", json={"branch": 1})
        failing = client.post(f"/v2/scenarios/{activity_id}/branches", json={
            "request_id": "req-branch-fail-01",
            "after_event_id": branch_point,
            "message": "这条分支先失败一次。",
        })
        assert failing.status_code == 502, failing.text
        assert failing.json()["detail"] == "SCENARIO_BRANCH_GENERATION_FAILED"
        failed_branch = next(item for item in client.get(
            f"/v2/scenarios/{activity_id}/branches").json()["branches"]
            if item["status"] == "FAILED")
        assert [item["event_type"] for item in client.get(
            f"/v2/scenarios/{activity_id}/branches/{failed_branch['branch_id']}"
        ).json()["events"]] == ["USER_MESSAGE"]
        branch_calls = kinds(tmp_path, "branch")
        replay = client.post(f"/v2/scenarios/{activity_id}/branches", json={
            "request_id": "req-branch-fail-01",
            "after_event_id": branch_point,
            "message": "这条分支先失败一次。",
        })
        assert replay.status_code == 502
        assert kinds(tmp_path, "branch") == branch_calls
        retried = client.post(
            f"/v2/scenarios/{activity_id}/branches/{failed_branch['branch_id']}/retry",
            json={"request_id": "req-branch-retry-01"})
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "READY"
        assert len(client.get(
            f"/v2/scenarios/{activity_id}/branches/{failed_branch['branch_id']}"
        ).json()["events"]) == 6
        assert kinds(tmp_path, "branch") == branch_calls + 1

        # --- ending is the only way into review ---------------------------
        version = client.get(f"/v2/scenarios/{activity_id}").json()["activity"]["version"]
        ended = client.post(f"/v2/activities/{activity_id}/end",
                            json={"request_id": "req-end-p12-01",
                                  "expected_version": version})
        assert ended.status_code == 200, ended.text
        assert ended.json()["activity"]["status"] == "ENDED"
        no_more = client.post(f"/v2/scenarios/{activity_id}/messages",
                              json={"request_id": "req-turn-after-end", "message": "结束之后不该再走"})
        assert no_more.status_code == 409

        # --- failed review is stored, replayed, then recoverable ----------
        client.post("/__e2e/faults", json={"review": 1})
        failed_review = client.post(f"/v2/scenarios/{activity_id}/review",
                                    json={"request_id": "req-review-fail-01"})
        assert failed_review.status_code == 502, failed_review.text
        assert failed_review.json()["detail"] == "SCENARIO_REVIEW_FAILED"
        state = client.get(f"/v2/scenarios/{activity_id}/review").json()
        assert state["status"] == "FAILED" and state["review"]["overview"] is None
        review_calls = kinds(tmp_path, "review")
        assert client.post(f"/v2/scenarios/{activity_id}/review",
                           json={"request_id": "req-review-fail-01"}).status_code == 502
        assert kinds(tmp_path, "review") == review_calls
        early = client.post(f"/v2/scenarios/{activity_id}/adopt", json={
            "request_id": "req-adopt-early", "branch_id": branch_a_id,
            "character_id": "rin"})
        assert early.status_code == 409
        assert early.json()["detail"] == "SCENARIO_REVIEW_NOT_READY"

        ready = client.post(f"/v2/scenarios/{activity_id}/review",
                            json={"request_id": "req-review-ok-01"})
        assert ready.status_code == 200, ready.text
        review = ready.json()
        assert review["status"] == "READY" and review["replayed"] is False
        assert review["review"]["strategy"].startswith("先说明")
        assert "妈妈担心收入风险" in review["review"]["feedback"]
        review_calls = kinds(tmp_path, "review")
        replay_review = client.post(f"/v2/scenarios/{activity_id}/review",
                                    json={"request_id": "req-review-other-01"})
        assert replay_review.status_code == 200
        assert replay_review.json()["replayed"] is True
        assert kinds(tmp_path, "review") == review_calls, "READY review is read, not regenerated"

        # --- only an explicit adoption writes one REAL shared memory ------
        adopted = client.post(f"/v2/scenarios/{activity_id}/adopt", json={
            "request_id": "req-adopt-ok-01", "branch_id": branch_a_id,
            "character_id": "rin"})
        assert adopted.status_code == 200, adopted.text
        adoption = adopted.json()
        assert adoption["replayed"] is False
        assert adoption["adoption"]["branch_id"] == branch_a_id
        assert adoption["real_experience_scope"] == {"schema_version": 1, "reality": "REAL"}
        audit = client.get("/__e2e/audit").json()
        assert audit["events"] == 1 and audit["memories"] == 1
        assert audit["practice_adopted_events"] == 1
        assert audit["scenario_adoptions"] == 1
        assert audit["bad_real_scope"] == 0
        assert audit["scenario_events_bad_scope"] == 0
        assert audit["unadopted_branch_leaks"] == {"memories": 0, "events": 0}
        memories = client.get("/v1/characters/rin/memories").json()["memories"]
        assert len(memories) == 1
        assert memories[0]["memory_type"] == "SHARED"
        assert "先说明自己的计划" in memories[0]["content"]
        assert "妈妈担心收入风险" in memories[0]["content"]
        assert "不是现实" in memories[0]["content"]

        same = client.post(f"/v2/scenarios/{activity_id}/adopt", json={
            "request_id": "req-adopt-same-02", "branch_id": branch_a_id,
            "character_id": "rin"})
        assert same.status_code == 200 and same.json()["replayed"] is True
        assert same.json()["event_id"] == adoption["event_id"]
        other = client.post(f"/v2/scenarios/{activity_id}/adopt", json={
            "request_id": "req-adopt-other-03", "branch_id": branch_b_id,
            "character_id": "rin"})
        assert other.status_code == 409
        assert other.json()["detail"] == "SCENARIO_ALREADY_ADOPTED"
        audit = client.get("/__e2e/audit").json()
        assert audit["events"] == 1 and audit["memories"] == 1
        assert audit["scenario_adoptions"] == 1
        assert audit["scenario_reviews"] == 1

        # --- the adopted practice is visible to the next real chat -------
        chat = client.post("/v1/chat", json={
            "character_id": "rin", "conversation_id": "p12-direct",
            "message": "我们上次练习的时候，我做得怎么样？"})
        assert chat.status_code == 200, chat.text
        react_prompts = [item["prompt"] for item in calls(tmp_path)
                         if item["kind"] == "react"]
        assert react_prompts, "the next chat must actually build a Person context"
        prompt = react_prompts[-1]
        assert "先说明自己的计划" in prompt
        assert "妈妈担心收入风险" in prompt
        assert "模拟关系练习" in prompt
        assert "如果我直接拒绝呢？" not in prompt
        assert "如果我说不出口" in prompt

        audit_after_chat = client.get("/__e2e/audit").json()
        before_restart = {"events": audit_after_chat["events"],
                          "memories": audit_after_chat["memories"],
                          "review": kinds(tmp_path, "review"),
                          "branch": kinds(tmp_path, "branch"),
                          "react": kinds(tmp_path, "react")}

    # --- restart: every durable state is read without a model call -------
    with server(tmp_path) as client:
        pids.append(client.get("/__e2e/audit").json()["pid"])
        scenario = client.get(f"/v2/scenarios/{activity_id}").json()
        assert scenario["activity"]["status"] == "ENDED"
        assert len(scenario["scenario"]["events"]) == 6
        assert len(scenario["scenario"]["branches"]) == 3
        assert scenario["scenario"]["adoption"]["branch_id"] == branch_a_id
        assert client.get(f"/v2/scenarios/{activity_id}/review").json()["status"] == "READY"
        audit = client.get("/__e2e/audit").json()
        assert audit["events"] == before_restart["events"]
        assert audit["memories"] == before_restart["memories"]
        assert audit["scenario_reviews"] == 1
        assert audit["scenario_adoptions"] == 1
        assert kinds(tmp_path, "review") == before_restart["review"]
        assert kinds(tmp_path, "branch") == before_restart["branch"]
        assert kinds(tmp_path, "react") == before_restart["react"]
        replayed_review = client.post(f"/v2/scenarios/{activity_id}/review",
                                      json={"request_id": "req-review-restart-01"})
        assert replayed_review.status_code == 200
        assert replayed_review.json()["replayed"] is True
        replayed_adopt = client.post(f"/v2/scenarios/{activity_id}/adopt", json={
            "request_id": "req-adopt-restart-01", "branch_id": branch_a_id,
            "character_id": "rin"})
        assert replayed_adopt.status_code == 200
        assert replayed_adopt.json()["replayed"] is True
        audit = client.get("/__e2e/audit").json()
        assert audit["events"] == before_restart["events"]
        assert audit["memories"] == before_restart["memories"]
        assert len(client.get("/v1/characters/rin/memories").json()["memories"]) == 1
    assert len(set(pids)) == 2
