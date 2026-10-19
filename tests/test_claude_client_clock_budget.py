import json
import os
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from character_memory.application.async_conversation import direct_channel
from character_memory.application.chat_service import ChatService
from character_memory.application.clock import RealClock
from character_memory.async_web import attach_async_routes
from character_memory.background_services import BackgroundServices
from character_memory.domain.models import ActionDecision, PersonReaction
from character_memory.llm.client import ModelCallResult, ModelCallTrace
from character_memory.config import Settings
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.capability_execution import CapabilityResult
from character_memory.runtime.direct_research import DirectResearchService
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore

class FakeClaude:
    def __init__(self): self.calls = []
    def run(self, request):
        self.calls.append(request)
        return CapabilityResult(request.request_id, 'SUCCESS', {
            'assistant_response':'compare color temperature',
            'knowledge_scope':'MODEL_RESPONSE',
            'execution':{'backend':'claude','usage_state':'UNKNOWN'},
        })

def setup(tmp_path):
    store = SQLiteStore(tmp_path / 'clock.sqlite')
    folder = tmp_path / 'personas' / 'echo'
    folder.mkdir(parents=True)
    (folder / 'persona.yaml').write_text('id: echo\nname: Echo\n', encoding='utf-8')
    (folder / 'capabilities.yaml').write_text('capabilities: [claude_task]\nskills: []\n', encoding='utf-8')
    profiles = lambda:[{'id':'echo','persona_path':str(folder / 'persona.yaml')}]
    runner = FakeClaude()
    settings = Settings(direct_claude_tasks_enabled=True, embedding_provider='deterministic', db_path=str(tmp_path / 'clock.sqlite'))
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, AlwaysDelegate(), 'isolated test person')
    runtime.research_service = DirectResearchService(settings, store, None, runner, profiles)
    return store, runtime, runner

class AlwaysDelegate:
    def __init__(self): self.prompts = []
    def react_call_for_session(self, context, session):
        self.prompts.append(context)
        delegate = '# Claude task delegation' in context and '# CLAUDE_TASK_RESULT' not in context
        action = ActionDecision(type='DELEGATE_TASK', task='compare lamp colors') if delegate else ActionDecision(type='MESSAGE', message='observed')
        return ModelCallResult(PersonReaction(actions=[action]), ModelCallTrace(request_messages=[], response_text='{}', model='fake-clock', attempt=1))

class ClientClockBudgetBypass(AssertionError):
    """Only the completely verified client-clock paid-budget bypass signature."""

@pytest.mark.parametrize('mode', ['count', 'money'])
def test_http_at_selects_paid_bucket_but_world_cursor_never_rewinds(tmp_path, monkeypatch, mode):
    # Offline guard: Starlette's ASGI TestClient does not need internet sockets.
    def no_network(*a, **k): raise AssertionError('external socket forbidden')
    monkeypatch.setattr(socket, 'create_connection', no_network)
    store, runtime, runner = setup(tmp_path)
    settings = runtime.research_service.settings
    settings.claude_execution_daily_max = 1 if mode == 'count' else 10
    settings.world_cost_saving_enabled = mode == 'money'
    settings.claude_execution_daily_budget_usd = .5 if mode == 'money' else None
    settings.claude_execution_max_budget_usd = .3 if mode == 'money' else None
    profiles = runtime.research_service.profiles
    bundle = SimpleNamespace(store=store, runtimes={'echo':runtime}, chat=ChatService(store, {'echo':runtime}, RealClock()), settings=settings)
    app = FastAPI()
    app.state.background_services = BackgroundServices()
    app.state.character_memory = SimpleNamespace(get_bundle=lambda:bundle, character_profiles=profiles, store=lambda:store, media_storage=None)
    attach_async_routes(app)
    scheduler = app.state.character_memory.reaction_scheduler
    scheduler.quiet_seconds = .001
    scheduler.max_burst_seconds = .002
    started = datetime.now().astimezone()
    today = started.replace(hour=12, minute=0, second=0, microsecond=0)
    rows = []
    try:
        with TestClient(app) as client:
            # No at = normal real time. Same-day explicit at blocks. Arbitrary
            # next dates buy a fresh bucket. Backfill changes event but not cursor.
            for at in [None, today, today + timedelta(days=10), today + timedelta(days=20), today - timedelta(days=10)]:
                body = {'character_id':'echo','conversation_id':'clock-proof','message':'please compare'}
                if at: body['at'] = at.isoformat()
                response = client.post('/v1/chat/messages', json=body)
                assert response.status_code == 202, response.text
                event_id = response.json()['event_id']
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    status = scheduler.status_snapshot(direct_channel('echo', 'clock-proof'))
                    if status['state']=='idle' and status['watermark'] >= event_id: break
                    time.sleep(.01)
                else: pytest.fail(f'scheduler timeout {status}')
                event = store.get_event(event_id)
                if at is not None:
                    assert event.event_time == at
                ledger = [dict(row) for row in store.conn.execute('SELECT request_id,status,started_at,execution_metadata_json FROM capability_executions ORDER BY rowid')]
                rows.append({'request_at':at.isoformat() if at else None,'http_status':response.status_code,'event_time':event.event_time.isoformat(),'world_time':store.get_world_time('echo').isoformat(),'runner_calls':len(runner.calls),'ledger':ledger})
            calls = [x['runner_calls'] for x in rows]
            unsafe_signature = calls == [1,1,2,3,4]
            if not unsafe_signature:
                assert calls == [1,1,1,1,1], 'same real day must retain a single paid quota bucket'
            assert rows[-1]['world_time'] == rows[-2]['world_time']
            assert datetime.fromisoformat(rows[0]['event_time']) >= started
            assert len({json.loads(x['execution_metadata_json'])['day'] for x in rows[-1]['ledger']}) == (4 if unsafe_signature else 1)
            if unsafe_signature:
                assert all(row['status'] == 'SUCCESS' for row in rows[-1]['ledger'])
                expected_days = [today.date(), (today + timedelta(days=10)).date(),
                                 (today + timedelta(days=20)).date(), (today - timedelta(days=10)).date()]
                assert [json.loads(row['execution_metadata_json'])['day'] for row in rows[-1]['ledger']] == [day.isoformat() for day in expected_days]
            assert client.post('/v1/conversations/example/messages',json={'message':'test'}).status_code == 404
            assert runtime.research_service.capabilities(event.model_copy(update={'metadata':{'channel':'GROUP'}})) == ()
        finished = datetime.now().astimezone()
        assert started.date() == finished.date(), 're-run if crossing midnight'
        if output := os.environ.get('CLOCK_EVIDENCE_DIR'):
            out = Path(output)
            out.mkdir(parents=True, exist_ok=True)
            (out / f'evidence-{mode}.json').write_text(json.dumps({'started':started.isoformat(),'finished':finished.isoformat(),'mode':mode,'offline_guard':True,'steps':rows},ensure_ascii=False,indent=2),encoding='utf-8')
        if unsafe_signature:
            raise ClientClockBudgetBypass('same real day bought four distinct count/money buckets via client at; calls=[1,1,2,3,4]')
    finally:
        scheduler.close()
        app.state.character_memory.stream_hub.close()
        store.close()
