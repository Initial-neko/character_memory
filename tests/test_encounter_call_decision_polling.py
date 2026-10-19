"""Issue #595: detail reads must not expire an in-flight call decision.

Known failures are restricted to the verified HTTP/state signature. Remove the
strict xfail markers when the decision ownership bug is fixed. Optional JSON
evidence goes outside the repository via ENCOUNTER_POLLING_EVIDENCE_DIR.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from character_memory.background_services import BackgroundServices
import character_memory.encounter as encounter_module
from character_memory.encounter_store import EncounterRepository
import character_memory.encounter_web as encounter_web
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 19, 4, tzinfo=timezone.utc)


class KnownPollingDecisionExpiry(RuntimeError):
    """Only the fully verified Issue #595 polling-expiry signature."""


KNOWN_POLLING_EXPIRY = pytest.mark.xfail(
    strict=True,
    raises=KnownPollingDecisionExpiry,
    reason="Issue #595: detail GET expires an otherwise valid slow call decision",
)


@pytest.fixture
def polling_store(tmp_path):
    store = SQLiteStore(tmp_path / "calls.db")
    try:
        repository = EncounterRepository(store)
        candidates = [
            repository.create_candidate(
                source_type="GENERATED",
                draft={"name": name},
                encounter_hook="hello",
                opening_message="hello",
                now=NOW,
            )
            for name in ("A", "B")
        ]
        yield store, repository, candidates
    finally:
        store.close()


@pytest.mark.parametrize(
    "poll",
    [
        pytest.param("none", id="no-get-control"),
        pytest.param("current", id="get-current", marks=KNOWN_POLLING_EXPIRY),
        pytest.param("other", id="get-other", marks=KNOWN_POLLING_EXPIRY),
    ],
)
def test_slow_call_decision_survives_detail_polling(polling_store, monkeypatch, poll):
    store, repository, (candidate, other) = polling_store
    elapsed = [0.0]
    model_result = {}
    started, release = threading.Event(), threading.Event()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(seconds=elapsed[0])

    monkeypatch.setattr(encounter_module, "datetime", Clock)
    monkeypatch.setattr(encounter_web, "datetime", Clock)
    monkeypatch.setattr(
        encounter_module, "time", SimpleNamespace(monotonic=lambda: elapsed[0])
    )

    def decide(*args):
        started.set()
        assert release.wait(10), "test model was not released"
        model_result.update(accept=True, elapsed=elapsed[0])
        return SimpleNamespace(accept=True, reason="yes")

    access = SimpleNamespace(
        read_store=store,
        settings=SimpleNamespace(api_key=""),
        character_profiles=lambda: [],
        require_bundle=lambda: SimpleNamespace(
            model=SimpleNamespace(structured_for_session=decide)
        ),
    )
    app = FastAPI()
    app.state.background_services = BackgroundServices()
    app.state.character_memory = access
    encounter_web.attach_encounter_routes(app)
    detail_status = None

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.post, f"/v1/encounters/{candidate['id']}/calls"
        )
        try:
            assert started.wait(10), "POST did not reach fake model"
            pending = repository.list_calls(candidate["id"])
            assert len(pending) == 1 and pending[0]["status"] == "RINGING"
            elapsed[0] = 100.0
            if poll != "none":
                polled = candidate if poll == "current" else other
                detail = client.get(f"/v1/encounters/{polled['id']}")
                detail_status = detail.status_code
                assert detail_status == 200, detail.text
                assert detail.json()["candidate"]["id"] == polled["id"]
            elapsed[0] = 120.0
        finally:
            release.set()
        response = future.result(timeout=10)
        calls = repository.list_calls(candidate["id"])
        current = repository.get_candidate(candidate["id"])
        evidence = {
            "poll": poll,
            "detail_status": detail_status,
            "post_status": response.status_code,
            "post_body": {key: value for key, value in response.json().items() if key != "session_token"},
            "session_token_present": bool(response.json().get("session_token")),
            "calls": calls,
            "candidate_status": current["status"],
            "model": model_result,
        }
        evidence_dir = os.environ.get("ENCOUNTER_POLLING_EVIDENCE_DIR")
        if evidence_dir:
            destination = Path(evidence_dir)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"polling-{poll}.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        if poll != "none" and response.status_code == 409:
            # Anything outside this proven signature is a real failure, never
            # an accepted xfail (including fixture/model/HTTP errors).
            assert detail_status == 200, evidence
            assert response.json() == {"detail": "电话邀请已过期或结束"}, evidence
            assert model_result == {"accept": True, "elapsed": 120.0}, evidence
            assert elapsed[0] == 120.0, evidence
            assert len(calls) == 1 and calls[0]["status"] == "EXPIRED", evidence
            assert calls[0]["connected_at"] is None, evidence
            assert current["status"] in {"NEW", "SEEN", "CHATTING"}, evidence
            assert repository.metrics(candidate["id"])["connected_calls"] == 0
            raise KnownPollingDecisionExpiry(json.dumps(evidence, ensure_ascii=False))

        assert response.status_code == 200, evidence
        body = response.json()
        assert body["status"] == "ACCEPTED", evidence
        connected = client.post(
            f"/v1/encounters/{candidate['id']}/calls/{body['id']}/session",
            json={"session_token": body["session_token"], "action": "CONNECTED"},
        )
        assert connected.status_code == 200, connected.text
        assert connected.json()["status"] == "CONNECTED"
