"""Encounter reply error ownership: isolated HTTP/SQLite regression evidence."""
import json
import hashlib
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.background_services import BackgroundServices
from character_memory.encounter_web import attach_encounter_routes
from character_memory.storage.sqlite import SQLiteStore


EVIDENCE = Path(os.environ.get('ENCOUNTER_AUDIT_JSON', 'artifacts/core-visual-evidence/encounter-stale-failure.json'))


class ConfirmedStaleFailure(AssertionError):
    pass


def exercise(tmp_path, *, fail, newer):
    store = SQLiteStore(str(tmp_path / 'isolated.db'))
    entered, release = threading.Event(), threading.Event()
    calls = []
    timeline = []

    def generate(prompt, schema, session):
        calls.append(prompt)
        if len(calls) == 1:
            timeline.append('U1_model_entered')
            entered.set()
            assert release.wait(5), 'audit barrier timed out'
            if fail:
                timeline.append('U1_model_failed_after_release')
                raise RuntimeError('audit: old generation failed')
            timeline.append('U1_model_succeeded_after_release')
        return SimpleNamespace(message='reply to current transcript')

    access = SimpleNamespace(read_store=store, settings=SimpleNamespace(api_key=''),
        character_profiles=lambda: [], require_bundle=lambda: SimpleNamespace(model=SimpleNamespace(structured_for_session=generate)))
    app = FastAPI()
    app.state.background_services = BackgroundServices()
    app.state.character_memory = access
    attach_encounter_routes(app)
    repo, scheduler = access.encounter_repository, access.encounter_scheduler
    candidate = repo.create_candidate(source_type='GENERATED', draft={'name':'Audit'},
        encounter_hook='fixture', opening_message='hello', now=datetime.now(timezone.utc))
    path = f'/v1/encounters/{candidate["id"]}'
    errors = []

    def drain():
        try:
            scheduler.drain_pending_replies(limit=1)
        except BaseException as exc:
            errors.append(repr(exc))

    worker = threading.Thread(target=drain)
    try:
        with TestClient(app) as client:
            first = client.post(path+'/messages', json={'message':'U1'})
            assert first.status_code == 200
            assert first.json()['reply_pending'] is True
            timeline.append('U1_HTTP_200_stored')
            worker.start()
            assert entered.wait(5), 'model was never entered'
            second = client.post(path+'/messages', json={'message':'U2'}) if newer else None
            if second is not None:
                assert second.status_code == 200
                assert second.json()['reply_pending'] is True
                prior = client.get(path).json()['candidate']
                assert prior['error'] == ''
                assert prior['reply_pending'] is True
                assert [m['content'] for m in prior['messages']] == ['U1','U2']
                timeline.append('U2_HTTP_200_stored_error_empty_pending_true')
            timeline.append('release_U1_model')
            release.set()
            worker.join(5)
            assert not worker.is_alive()
            assert not errors, errors
            timeline.append('U1_worker_finished')
            detail = client.get(path).json()['candidate']
            recovered = scheduler.recover_pending_replies()
            timeline.append(f'durable_recovery_count_{recovered}')
            before_retry = {'roles':[m['role'] for m in detail['messages']],
                'contents':[m['content'] for m in detail['messages']], 'error':detail['error'],
                'reply_pending':detail['reply_pending'], 'recovered':recovered,
                'pending_count':scheduler.pending_reply_count(), 'model_calls':len(calls)}
            if fail:
                retry = client.post(path+'/reply/retry')
                assert retry.status_code == 200
                timeline.append('manual_retry_HTTP_200')
            scheduler.drain_pending_replies()
            after = client.get(path).json()['candidate']
            assert after['error'] == ''
            assert after['messages'][-1]['role'] == 'CHARACTER'
            root = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
            source_paths = ['src/character_memory/encounter.py',
                'src/character_memory/encounter_store.py','src/character_memory/encounter_web.py']
            result = {'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
                'source_sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in source_paths},
                'boundary':{'http':'TestClient in-process ASGI','store':'isolated SQLite',
                    'provider':'barrier fake; no live model initialized','formal_data':'untouched',
                    'scheduling':'manual drain/recover, not continuous threads'},
                'timeline':timeline,'fail':fail,'newer':newer,'http_first':first.status_code,
                'http_second':second.status_code if second is not None else None,
                'before_retry':before_retry,'after_retry_or_recovery':{
                    'roles':[m['role'] for m in after['messages']], 'error':after['error'],
                    'model_calls':len(calls)}}
            EVIDENCE.parent.mkdir(parents=True,exist_ok=True)
            existing = json.loads(EVIDENCE.read_text(encoding='utf-8')) if EVIDENCE.exists() else []
            existing.append(result)
            EVIDENCE.write_text(json.dumps(existing,ensure_ascii=False,indent=2),encoding='utf-8')
            return result
    finally:
        release.set()
        if worker.is_alive():
            worker.join(5)
        store.close()


def test_newer_line_recovers_after_stale_success(tmp_path):
    result = exercise(tmp_path,fail=False,newer=True)
    assert result['before_retry']['error'] == ''
    assert result['before_retry']['recovered'] == 1
    assert result['before_retry']['roles'] == ['USER','USER']


def test_current_failure_is_retryable(tmp_path):
    result = exercise(tmp_path,fail=True,newer=False)
    assert result['before_retry']['error']
    assert result['before_retry']['recovered'] == 0
    assert result['before_retry']['roles'] == ['USER']


def test_stale_failure_does_not_poison_newer_line(tmp_path):
    result = exercise(tmp_path,fail=True,newer=True)
    observed = result['before_retry']
    # A failure that belongs to a superseded line must not rewrite its retry state.
    if (observed['roles'] == ['USER','USER'] and observed['contents'] == ['U1','U2']
        and observed['error'] == '角色暂时没有回应，可以稍后重试。'
        and observed['recovered'] == 0 and observed['pending_count'] == 0
        and observed['reply_pending'] is False and observed['model_calls'] == 1):
        raise ConfirmedStaleFailure('stale U1 failure blocks U2 recovery')
    assert observed['error'] == ''
    assert observed['recovered'] == 1


def test_transient_read_failure_clears_in_flight(tmp_path):
    """A read failure before the model call must not strand the candidate."""
    store = SQLiteStore(str(tmp_path / 'in-flight.db'))
    access = SimpleNamespace(read_store=store, settings=SimpleNamespace(api_key=''),
        character_profiles=lambda: [],
        require_bundle=lambda: SimpleNamespace(model=SimpleNamespace()))
    app = FastAPI()
    app.state.background_services = BackgroundServices()
    app.state.character_memory = access
    attach_encounter_routes(app)
    repo, scheduler = access.encounter_repository, access.encounter_scheduler
    candidate = repo.create_candidate(source_type='GENERATED', draft={'name': 'Audit'},
        encounter_hook='fixture', opening_message='hello', now=datetime.now(timezone.utc))
    try:
        scheduler.enqueue_reply(candidate['id'])
        original = repo.list_messages
        calls = []

        def flaky(candidate_id, limit=1):
            calls.append(candidate_id)
            if len(calls) == 1:
                raise RuntimeError('transient read failure')
            return original(candidate_id, limit=limit)

        repo.list_messages = flaky
        assert scheduler.drain_pending_replies(limit=1) == []
        assert candidate['id'] not in scheduler._in_flight
        assert scheduler.pending_reply_count() == 0
        # The durable line is still recoverable rather than stuck forever.
        assert scheduler.enqueue_reply(candidate['id']) is True
    finally:
        store.close()

@pytest.mark.parametrize("superseded_during_generation", [False, True])
def test_failure_owns_actual_model_transcript_after_scheduler_snapshot(
    tmp_path, superseded_during_generation
):
    """A line arriving before generation belongs to that attempt, not its queue snapshot."""
    store = SQLiteStore(str(tmp_path / "reply-owner.db"))
    prompts = []
    access = SimpleNamespace(
        read_store=store, settings=SimpleNamespace(api_key=""),
        character_profiles=lambda: [],
    )
    app = FastAPI()
    app.state.background_services = BackgroundServices()
    app.state.character_memory = access
    attach_encounter_routes(app)
    repo, scheduler = access.encounter_repository, access.encounter_scheduler
    candidate = repo.create_candidate(
        source_type="GENERATED", draft={"name": "Audit"},
        encounter_hook="fixture", opening_message="hello",
        now=datetime.now(timezone.utc),
    )
    candidate_id = candidate["id"]
    service = scheduler.service
    calls = []

    def generate(prompt, schema, session):
        prompts.append(prompt)
        assert "用户: U2" in prompt
        if superseded_during_generation:
            service.post_message(candidate_id, "U3")
        raise RuntimeError("current U2 provider failure")

    access.require_bundle = lambda: SimpleNamespace(
        model=SimpleNamespace(structured_for_session=generate)
    )
    original_list = repo.list_messages

    def insert_between_snapshots(key, *, limit=30):
        snapshot = original_list(key, limit=limit)
        if key == candidate_id and limit == 1 and not calls:
            calls.append("scheduler-read-U1")
            assert snapshot[-1]["content"] == "U1"
            # Deterministically interleave a real durable post between the
            # scheduler ownership read and the service's model transcript.
            service.post_message(candidate_id, "U2")
        return snapshot

    try:
        service.post_message(candidate_id, "U1")
        repo.list_messages = insert_between_snapshots
        assert scheduler.enqueue_reply(candidate_id)
        assert scheduler.drain_pending_replies(limit=1) == []
        assert len(prompts) == 1
        assert calls == ["scheduler-read-U1"]
        assert candidate_id not in scheduler._in_flight
        current = repo.get_candidate(candidate_id)
        messages = repo.list_messages(candidate_id)
        assert [m["content"] for m in messages] == (
            ["U1", "U2", "U3"] if superseded_during_generation else ["U1", "U2"]
        )
        if superseded_during_generation:
            assert current["error"] == ""
            assert scheduler.recover_pending_replies() == 1
        else:
            assert current["error"], "The actual U2 failure must require explicit retry"
            assert scheduler.recover_pending_replies() == 0
    finally:
        store.close()
