"""Real HTTP acceptance: practice, distinct views, suggestion attribution, isolation.

A temp SQLite database and temp persona root back real uvicorn processes. The
deterministic model writes one JSON line per call so "reads cost zero calls" and
"a failed round is not retried" are observed counts, not assumptions.
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


def create_body(request_id="req-scenario-0001"):
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


def scenario_calls(root: Path) -> list[dict]:
    return [item for item in calls(root) if item["kind"] == "scenario"]


def test_relationship_practice_over_real_http(tmp_path):
    pids = []
    with server(tmp_path) as client:
        pids.append(client.get("/__e2e/audit").json()["pid"])
        created = client.post("/v2/scenarios", json=create_body())
        assert created.status_code == 201, created.text
        payload = created.json()
        activity = payload["activity"]
        activity_id = activity["id"]
        assert activity["status"] == "CREATED" and activity["version"] == 1
        assert payload["scenario"]["situation"].startswith("我正在考虑辞掉")
        assert payload["experience_scope"] == {
            "schema_version": 1, "reality": "SIMULATED", "activity_id": activity_id}
        assert [item["participant_key"] for item in payload["scenario"]["events"]] == []
        # A simulated practice partner never becomes a Person.
        listed = {item["id"] for item in client.get("/v1/characters").json()["characters"]}
        assert listed == set(PERSONAS)
        assert client.get(f"/v2/scenarios/{activity_id}").status_code == 200

        resumed = client.post(f"/v2/activities/{activity_id}/resume",
                              json={"request_id": "req-start-0001", "expected_version": 1})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["activity"]["status"] == "ACTIVE"

        # --- one complete practice turn ---------------------------------
        first = client.post(f"/v2/scenarios/{activity_id}/messages",
                            json={"request_id": "req-turn-0001",
                                  "message": "我今晚想先讲我的计划，希望你们先听我说完。"})
        assert first.status_code == 200, first.text
        turn = first.json()
        assert turn["replayed"] is False
        assert len(turn["perspectives"]) == 3
        stances = [item["stance"] for item in turn["perspectives"]]
        assert len(set(stances)) == 3, stances
        # The simulated partners give different, disagreeing views and answer
        # the user in role; formal partners do not answer as the counterpart.
        assert {"先照顾自己", "担心风险", "先算成本"} <= set(stances)
        assert {item["participant_key"] for item in turn["replies"]} == {"sim:mother", "sim:sister"}
        assert all(item["actor_type"] == "SIMULATED_CHARACTER" for item in turn["replies"])
        assert [item["event_type"] for item in turn["events"]] == [
            "USER_MESSAGE", "PERSPECTIVE", "PERSPECTIVE", "PERSPECTIVE", "REPLY", "REPLY"]
        for event in turn["events"]:
            assert event["experience_scope"] == turn["experience_scope"]
        assert turn["suggestion_ref"]["kind"] == "SCENARIO_EVENT"
        assert turn["suggestion_ref"]["activity_id"] == activity_id

        transcript = client.get(f"/v2/scenarios/{activity_id}").json()
        assert len(transcript["scenario"]["events"]) == 6
        assert transcript["activity"]["status"] == "ACTIVE"
        assert len(scenario_calls(tmp_path)) == 1

        # --- drafts reuse the existing suggestion pipeline ---------------
        suggestion_request = {
            "request_id": str(uuid4()),
            "context_ref": turn["suggestion_ref"],
            "expected_profile_version": 0,
        }
        batch_response = client.post("/v2/reply-suggestions", json=suggestion_request)
        assert batch_response.status_code == 200, batch_response.text
        batch = batch_response.json()
        assert batch["status"] == "READY" and len(batch["suggestions"]) == 3
        choice = batch["suggestions"][0]
        sent = client.post(f"/v2/scenarios/{activity_id}/messages",
                           json={"request_id": "req-turn-0002", "message": choice["text"],
                                 "reply_suggestion": {"batch_id": batch["batch_id"],
                                                      "suggestion_id": choice["id"]}})
        assert sent.status_code == 200, sent.text
        assert len(scenario_calls(tmp_path)) == 2
        signals = client.get(f"/v2/reply-suggestions/{batch['batch_id']}/signals").json()["signals"]
        assert [item["kind"] for item in signals] == ["SELECT"]
        assert signals[0]["source_ref"]["kind"] == "SCENARIO_EVENT"
        # An edited draft is classified EDIT, and plain free input FREE_INPUT;
        # both reuse the same accepted-turn path rather than a new input mode.
        edited_batch = client.post("/v2/reply-suggestions", json={
            **suggestion_request, "request_id": str(uuid4()),
            "context_ref": sent.json()["suggestion_ref"],
        }).json()
        edited = client.post(f"/v2/scenarios/{activity_id}/messages",
                             json={"request_id": "req-turn-0003",
                                   "message": "我会自己改一句：我们先吃饭，吃完再算账。",
                                   "reply_suggestion": {"batch_id": edited_batch["batch_id"],
                                                        "suggestion_id": edited_batch["suggestions"][0]["id"]}})
        assert edited.status_code == 200, edited.text
        edited_signals = client.get(
            f"/v2/reply-suggestions/{edited_batch['batch_id']}/signals").json()["signals"]
        assert [item["kind"] for item in edited_signals] == ["EDIT"]

        # --- failures are explicit and never fabricate partner facts ------
        before = len(calls(tmp_path))
        failed = client.post(f"/v2/scenarios/{activity_id}/messages",
                             json={"request_id": "req-turn-fail", "message": "FAIL_SCENARIO 这一句会失败"})
        assert failed.status_code == 502 and failed.json()["detail"] == "SCENARIO_GENERATION_FAILED"
        assert len(calls(tmp_path)) == before + 1
        replay = client.post(f"/v2/scenarios/{activity_id}/messages",
                             json={"request_id": "req-turn-fail", "message": "FAIL_SCENARIO 这一句会失败"})
        assert replay.status_code == 502
        assert len(calls(tmp_path)) == before + 1, "a failed round must not be retried automatically"
        after_failure = client.get(f"/v2/scenarios/{activity_id}").json()["scenario"]
        failed_round = [item for item in after_failure["rounds"] if item["status"] == "FAILED"]
        assert failed_round, after_failure["rounds"]
        failed_events = [item for item in after_failure["events"] if item["event_type"] == "PERSPECTIVE"]
        assert len(failed_events) == 9  # 3 turns x 3 perspectives; the failed turn added none

        duplicate = client.post(f"/v2/scenarios/{activity_id}/messages",
                                json={"request_id": "req-turn-dup", "message": "DUPLICATE_VIEWS 这一句"})
        assert duplicate.status_code == 502
        duplicate_events = [item for item in client.get(
            f"/v2/scenarios/{activity_id}").json()["scenario"]["events"]
                            if item["event_type"] == "PERSPECTIVE"]
        assert len(duplicate_events) == 9

        # Pausing the scene stops new simulated consequences.
        paused = client.post(f"/v2/activities/{activity_id}/pause",
                             json={"request_id": "req-pause-0001", "expected_version": 2})
        assert paused.status_code == 200
        before = len(calls(tmp_path))
        refused = client.post(f"/v2/scenarios/{activity_id}/messages",
                              json={"request_id": "req-turn-paused", "message": "暂停后不该有回应"})
        assert refused.status_code == 409 and refused.json()["detail"] == "SCENARIO_NOT_ACTIVE"
        assert len(calls(tmp_path)) == before

        # --- no real memory / real context pollution --------------------
        audit = client.get("/__e2e/audit").json()
        assert audit["scenario_events"] > 0
        assert audit["scenario_events_bad_scope"] == 0
        assert audit["memories"] == 0
        assert audit["real_leaks"] == {"memories": 0, "events": 0, "conversation_events": 0}
        assert client.get("/v1/characters/rin/memories").json()["memories"] == []

        # A real Direct fact and its draft must never see the practice text,
        # and the practice prompt must never see the real fact.
        seeded = client.post("/__e2e/seed-real-fact",
                             json={"content": "真实私密：我其实还有一笔存款"}).json()
        real_batch = client.post("/v2/reply-suggestions", json={
            "request_id": str(uuid4()),
            "context_ref": {"kind": "EVENT", "id": seeded["event_id"]},
            "expected_profile_version": 0,
        })
        assert real_batch.status_code == 200, real_batch.text
        logged = calls(tmp_path)
        suggestion_prompts = [item["prompt"] for item in logged if item["kind"] == "suggestions"]
        scenario_prompts = [item["prompt"] for item in logged if item["kind"] == "scenario"]
        real_prompt = next(prompt for prompt in reversed(suggestion_prompts)
                           if "真实私密" in prompt)
        assert "辞职" not in real_prompt and "sim:mother" not in real_prompt
        assert all("真实私密" not in prompt for prompt in scenario_prompts)

        version_before_restart = client.get(f"/v2/scenarios/{activity_id}").json()["activity"]["version"]
        event_count = len(client.get(f"/v2/scenarios/{activity_id}").json()["scenario"]["events"])

    # Reads and lifecycle state survive a restart; no model call is made.
    before_restart_calls = len(calls(tmp_path))
    with server(tmp_path) as client:
        pids.append(client.get("/__e2e/audit").json()["pid"])
        restored = client.get(f"/v2/scenarios/{activity_id}")
        assert restored.status_code == 200, restored.text
        body = restored.json()
        assert body["activity"]["status"] == "PAUSED"
        assert body["activity"]["version"] == version_before_restart
        assert len(body["scenario"]["events"]) == event_count
        assert body["experience_scope"]["reality"] == "SIMULATED"
        assert len(calls(tmp_path)) == before_restart_calls
        resumed = client.post(f"/v2/activities/{activity_id}/resume",
                              json={"request_id": "req-resume-after-restart",
                                    "expected_version": version_before_restart})
        assert resumed.status_code == 200
        continued = client.post(f"/v2/scenarios/{activity_id}/messages",
                                json={"request_id": "req-turn-after-restart",
                                      "message": "我们改天再谈，今天先吃饭。"})
        assert continued.status_code == 200, continued.text
        assert len(continued.json()["perspectives"]) == 3
    assert len(set(pids)) == 2
