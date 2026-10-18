from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from character_memory.encounter import EncounterService
from character_memory.encounter_store import EncounterRepository
from character_memory.storage.sqlite import SQLiteStore


@pytest.fixture
def encounter(tmp_path):
    store = SQLiteStore(str(tmp_path / 'calls.db'))
    repo = EncounterRepository(store)
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    c = repo.create_candidate(source_type='GENERATED', draft={'name':'铃'}, encounter_hook='hi', opening_message='你好', now=now)
    model = SimpleNamespace(structured_for_session=lambda *a: SimpleNamespace(accept=False, reason='正在录音，稍后再聊。'))
    access = SimpleNamespace(require_bundle=lambda:SimpleNamespace(model=model))
    yield EncounterService(access,repo), repo, c, now, model
    store.close()


def test_call_refusal_is_durable_and_not_a_connected_call(encounter):
    service,repo,c,now,model=encounter
    result=service.request_call(c['id'],now=now)
    assert result['status']=='DECLINED'
    assert '录音' in result['reason']
    assert 'session_token' not in result
    assert repo.metrics(c['id'])['connected_calls']==0
    assert repo.list_calls(c['id'])[0]['status']=='DECLINED'


def test_shared_experiences_require_a_character_reply(encounter):
    service,repo,c,now,model=encounter
    repo.append_message(c['id'],'USER','你好',now)
    metrics=repo.metrics(c['id'])
    assert metrics['user_messages']==1
    assert metrics['character_replies']==0
    assert metrics['mutual_turns']==0
    # One-sided speech is not yet an experience the two of them shared.
    assert metrics['shared_experiences']==0
    repo.append_message(c['id'],'CHARACTER','你好呀。',now+timedelta(seconds=1))
    assert repo.metrics(c['id'])['shared_experiences']==1


def test_call_session_requires_token_and_actual_connection(encounter):
    service,repo,c,now,model=encounter
    model.structured_for_session=lambda *a:SimpleNamespace(accept=True,reason='现在可以聊。')
    call=service.request_call(c['id'],now=now)
    assert call['status']=='ACCEPTED'
    assert repo.metrics(c['id'])['connected_calls']==0
    with pytest.raises(ValueError):
        repo.update_call(call['id'],'wrong','CONNECTED',now)
    repo.update_call(call['id'],call['session_token'],'CONNECTED',now)
    repo.update_call(call['id'],call['session_token'],'CONNECTED',now+timedelta(seconds=10))
    assert repo.metrics(c['id'])['connected_calls']==1
    repo.update_call(call['id'],call['session_token'],'ENDED',now+timedelta(seconds=20))
    assert repo.list_calls(c['id'])[0]['status']=='ENDED'
    assert 'token' not in repo.list_calls(c['id'])[0]


def test_dismiss_during_call_decision_cannot_connect(encounter):
    service,repo,c,now,model=encounter
    def decide(*args):
        repo.set_status(c['id'],'DISMISSED',now)
        return SimpleNamespace(accept=True,reason='好')
    model.structured_for_session=decide
    with pytest.raises(ValueError):
        service.request_call(c['id'],now=now)
    assert repo.metrics(c['id'])['connected_calls']==0
    assert repo.list_calls(c['id'])[0]['status']=='CANCELLED'


def test_static_status_route_survives_candidate_detail_route(encounter):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from character_memory.encounter_web import attach_encounter_routes
    service,repo,c,now,model=encounter
    app=FastAPI()
    from character_memory.background_services import BackgroundServices
    app.state.background_services=BackgroundServices()
    app.state.character_memory=SimpleNamespace(read_store=repo.store,settings=SimpleNamespace(api_key=''),
        character_profiles=lambda:[],require_bundle=lambda:SimpleNamespace(model=model))
    attach_encounter_routes(app)
    client=TestClient(app)
    assert client.get('/v1/encounters/status').status_code==200
    assert client.get(f'/v1/encounters/{c["id"]}').json()['candidate']['id']==c['id']
