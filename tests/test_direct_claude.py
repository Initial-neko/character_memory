from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from character_memory.config import Settings
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, PersonReaction
from character_memory.llm.client import ModelCallResult, ModelCallTrace
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime, SupersededReaction
from character_memory.runtime.capability_execution import CapabilityResult
from character_memory.runtime.direct_research import DirectResearchService
from character_memory.storage.sqlite import SQLiteStore


class TaskModel:
    def __init__(self, *, delegate=True, recurse=False):
        self.prompts = []
        self.delegate, self.recurse = delegate, recurse
    def react_call_for_session(self, context, session):
        self.prompts.append(context)
        use_tool = self.delegate and (len(self.prompts) == 1 or self.recurse)
        action = ActionDecision(type='DELEGATE_TASK', task='解释两种灯光设计的差别') if use_tool else ActionDecision(type='MESSAGE', message='执行器给出的建议是先对比色温；这是分析建议，不是现场观察。')
        return ModelCallResult(PersonReaction(actions=[action]), ModelCallTrace(request_messages=[], response_text='{}', model='test', attempt=1))


class Runner:
    def __init__(self): self.calls = []
    def run(self, request):
        self.calls.append(request)
        return CapabilityResult(request.request_id, 'SUCCESS', {'assistant_response':'先比较色温与亮度。',
            'knowledge_scope':'MODEL_RESPONSE', 'execution':{'backend':'claude','usage_state':'UNKNOWN'}})


def setup(tmp_path, *, grant=True, delegate=True, recurse=False):
    store = SQLiteStore(tmp_path/'db.sqlite')
    folder = tmp_path/'personas'/'echo'
    folder.mkdir(parents=True)
    (folder/'persona.yaml').write_text('id: echo\nname: Echo\n', encoding='utf-8')
    if grant: (folder/'capabilities.yaml').write_text('capabilities: [claude_task]\nskills: []\n',encoding='utf-8')
    runner = Runner()
    settings = Settings(direct_claude_tasks_enabled=True, embedding_provider='deterministic')
    service = DirectResearchService(settings, store, None, runner, lambda:[{'id':'echo','persona_path':str(folder/'persona.yaml')}])
    emb=DeterministicEmbedding()
    model=TaskModel(delegate=delegate,recurse=recurse)
    runtime=PersonRuntime(store, VectorRecall(store,emb),emb,model,'自知是AI、愿意帮忙测试的Echo。')
    runtime.research_service=service
    event=Event(character_id='echo', event_type=EventType.USER_MESSAGE,event_time=datetime.now(timezone.utc),content='请帮我分析两种灯光设计',metadata={'conversation_id':'echo:default','channel':'DIRECT'})
    return store,runtime,runner,model,event


def test_granted_person_delegates_once_then_interprets_durable_result(tmp_path):
    store,runtime,runner,model,event=setup(tmp_path)
    result=runtime.handle(event)
    assert len(runner.calls)==1 and len(model.prompts)==2
    assert runner.calls[0].channel=='DIRECT' and runner.calls[0].capability=='CLAUDE_TASK'
    assert runner.calls[0].arguments=={'query':'解释两种灯光设计的差别'}
    assert 'CLAUDE_TASK_RESULT' in model.prompts[1]
    assert result.reaction.actions[0].type==ActionType.MESSAGE
    assert store.conn.execute('SELECT count(*) FROM capability_executions').fetchone()[0]==1
    assert '先比较色温' in store.conn.execute('SELECT result_json FROM capability_executions').fetchone()[0]
    store.close()


@pytest.mark.parametrize('grant,delegate',[(False,True),(True,False)])
def test_ungranted_or_ordinary_turn_never_calls_claude(tmp_path,grant,delegate):
    store,runtime,runner,model,event=setup(tmp_path,grant=grant,delegate=delegate)
    runtime.handle(event)
    assert not runner.calls and len(model.prompts)==1
    if not grant: assert '# Claude task delegation' not in model.prompts[0]
    store.close()


def test_result_cannot_delegate_again_and_stale_work_does_not_respond(tmp_path):
    store,runtime,runner,model,event=setup(tmp_path,recurse=True)
    result=runtime.handle(event)
    assert len(runner.calls)==1 and not any(a.type.value=='DELEGATE_TASK' for a in result.reaction.actions)
    store.close()


def test_permission_revocation_and_supersession_prevent_launch(tmp_path):
    store,runtime,runner,model,event=setup(tmp_path)
    with pytest.raises(SupersededReaction): runtime.handle(event,commit_guard=lambda:False)
    assert not runner.calls
    store.close()


def test_world_grant_does_not_grant_direct_and_malformed_yaml_fails_closed(tmp_path):
    store,runtime,runner,model,event=setup(tmp_path)
    path=tmp_path/'personas'/'echo'/'capabilities.yaml'
    path.write_text('capabilities: [claude_research]\n',encoding='utf-8')
    assert runtime.research_service.grant(event) is None
    path.write_text('capabilities: [broken\n',encoding='utf-8')
    assert runtime.research_service.grant(event) is None
    runtime.handle(event)
    assert not runner.calls
    store.close()


def test_supersession_after_cli_keeps_receipt_without_interpretation(tmp_path):
    store,runtime,runner,model,event=setup(tmp_path)
    current=[True]
    run=runner.run
    def complete(request):
        result=run(request)
        current[0]=False
        return result
    runner.run=complete
    with pytest.raises(SupersededReaction):
        runtime.handle(event,commit_guard=lambda:current[0])
    assert len(model.prompts)==1 and len(runner.calls)==1
    assert store.conn.execute("SELECT status FROM capability_executions").fetchone()[0]=='SUCCESS'
    assert store.conn.execute("SELECT count(*) FROM events WHERE event_type='CHARACTER_MESSAGE'").fetchone()[0]==0
    store.close()


def test_discovery_uses_real_direct_boundary_without_writes_or_model_calls(tmp_path):
    store, runtime, runner, model, event = setup(tmp_path)
    service = runtime.research_service
    before = store.conn.total_changes
    capabilities = service.capabilities(event)
    assert [item.id for item in capabilities] == ['CLAUDE_TASK']
    assert service.grant(event) == []
    assert store.conn.total_changes == before
    assert model.prompts == [] and runner.calls == []
    for metadata in ({'channel':'GROUP'}, {'conversation_id':'group:1'},
                     {'source_conversation_event_id':1}, {'group_event_id':1}):
        assert service.capabilities(event.model_copy(update={'metadata':metadata})) == ()
    assert service.capabilities(event.model_copy(update={'event_type':EventType.TIME_TICK})) == ()
    store.close()


def test_unavailable_runner_is_hidden_and_model_cannot_force_delegation(tmp_path):
    store, runtime, runner, model, event = setup(tmp_path)
    runtime.research_service.executor.claude_runner = None
    runtime.handle(event)
    assert '# Claude task delegation' not in model.prompts[0]
    assert len(model.prompts) == 1 and not runner.calls
    assert store.conn.execute('SELECT count(*) FROM capability_executions').fetchone()[0] == 0
    store.close()


def test_exhausted_claude_daily_quota_is_hidden_in_next_turn(tmp_path):
    store, runtime, runner, model, event = setup(tmp_path)
    runtime.research_service.settings.claude_execution_daily_max = 1
    runtime.handle(event)
    assert len(runner.calls) == 1
    before = store.conn.total_changes
    assert runtime.research_service.capabilities(event) == ()
    assert store.conn.total_changes == before
    assert len(runner.calls) == 1
    store.close()


def test_revocation_after_prompt_discovery_blocks_execution(tmp_path):
    store, runtime, runner, model, event = setup(tmp_path)
    original = model.react_call_for_session
    def revoke(context, session):
        assert '# Claude task delegation' in context
        (tmp_path/'personas/echo/capabilities.yaml').write_text('capabilities: []\n', encoding='utf-8')
        return original(context, session)
    model.react_call_for_session = revoke
    result = runtime.handle(event)
    assert not runner.calls and len(model.prompts) == 1
    assert not any(a.type == ActionType.DELEGATE_TASK for a in result.reaction.actions)
    store.close()


def test_invalid_utf8_grant_fails_closed_in_real_direct_path(tmp_path):
    store, runtime, runner, model, event = setup(tmp_path)
    (tmp_path/'personas/echo/capabilities.yaml').write_bytes(b'\xff\xfe')
    runtime.handle(event)
    assert not runner.calls and len(model.prompts) == 1
    assert '# Claude task delegation' not in model.prompts[0]
    store.close()


def test_runner_readiness_is_rechecked_after_model_decides(tmp_path):
    store, runtime, runner, model, event = setup(tmp_path)
    ready = [True]
    runner.is_available = lambda: ready[0]
    original = model.react_call_for_session
    def stop_executor(context, session):
        assert '# Claude task delegation' in context
        ready[0] = False
        return original(context, session)
    model.react_call_for_session = stop_executor
    result = runtime.handle(event)
    assert not runner.calls and len(model.prompts) == 1
    assert not any(a.type == ActionType.DELEGATE_TASK for a in result.reaction.actions)
    assert store.conn.execute('SELECT count(*) FROM capability_executions').fetchone()[0] == 0
    store.close()


def test_hidden_daily_budget_still_allows_an_ordinary_next_turn(tmp_path):
    store, runtime, runner, model, event = setup(tmp_path)
    settings = runtime.research_service.settings
    settings.world_cost_saving_enabled = True
    settings.claude_execution_daily_budget_usd = .5
    settings.claude_execution_max_budget_usd = .3
    runtime.handle(event)
    assert len(runner.calls) == 1
    # The runner returned UNKNOWN usage; the .3 reservation remains chargeable.
    before = store.conn.total_changes
    assert runtime.research_service.capabilities(event) == ()
    assert store.conn.total_changes == before
    model.prompts.clear()
    result = runtime.handle(event.model_copy(update={'id':None, 'content':'继续普通聊天'}))
    assert len(model.prompts) == 1 and len(runner.calls) == 1
    assert '# Claude task delegation' not in model.prompts[0]
    assert not any(a.type == ActionType.DELEGATE_TASK for a in result.reaction.actions)
    assert store.conn.execute('SELECT count(*) FROM capability_executions').fetchone()[0] == 1
    store.close()


def test_discovery_quota_hint_does_not_replace_atomic_dispatch_check(tmp_path):
    store, runtime, runner, model, event = setup(tmp_path)
    service = runtime.research_service
    service.settings.claude_execution_daily_max = 1
    before = store.conn.total_changes
    assert service.capabilities(event)
    assert service.capabilities(event)
    assert store.conn.total_changes == before
    # A competing real reservation occurs after discovery but before execution.
    original = model.react_call_for_session
    def exhaust(context, session):
        store.conn.execute("INSERT INTO capability_executions (request_id,character_id,opportunity_id,request_json,status,started_at,completed_at,result_json) VALUES (?,?,?,?,?,?,?,?)",
            ('competitor','echo','other','{}','STARTED',event.event_time.isoformat(),None,None))
        store.conn.execute("UPDATE capability_executions SET execution_metadata_json=? WHERE request_id='competitor'",
            ('{"backend":"claude","day":"'+event.event_time.astimezone().date().isoformat()+'"}',))
        store.conn.commit()
        return original(context, session)
    model.react_call_for_session = exhaust
    result = runtime.handle(event)
    assert not runner.calls and len(model.prompts) == 1
    assert not any(a.type == ActionType.DELEGATE_TASK for a in result.reaction.actions)
    store.close()


@pytest.mark.parametrize('unavailable', [False, True])
def test_completed_same_event_receipt_remains_readable_after_quota_or_runner_loss(tmp_path, unavailable):
    store, runtime, runner, model, event = setup(tmp_path)
    runtime.research_service.settings.claude_execution_daily_max = 1
    result = runtime.handle(event)
    assert len(runner.calls) == 1
    if unavailable:
        runtime.research_service.executor.claude_runner = None
    before = store.conn.total_changes
    assert runtime.research_service.capabilities(result.event)
    assert store.conn.total_changes == before
    model.prompts.clear()
    repeated = runtime.handle(result.event, persist_event=False)
    assert len(runner.calls) == 1 and len(model.prompts) == 0
    assert repeated.replayed is True
    assert repeated.reaction.actions[0].type == ActionType.MESSAGE
    assert store.conn.execute('SELECT count(*) FROM capability_executions').fetchone()[0] == 1
    assert store.conn.execute("SELECT count(*) FROM events WHERE json_extract(metadata_json,'$.fact_kind')='CLAUDE_TASK_RESULT'").fetchone()[0] == 1
    store.close()


def test_cached_execution_still_requires_current_role_and_identical_payload(tmp_path):
    store, runtime, runner, model, event = setup(tmp_path)
    runtime.research_service.settings.claude_execution_daily_max = 1
    result = runtime.handle(event)
    model.prompts.clear()
    original = model.react_call_for_session
    # Keep the normal result interpreter, but change only the first task query.
    def changed(context, session):
        response = original(context, session)
        if len(model.prompts) == 1:
            return ModelCallResult(PersonReaction(actions=[ActionDecision(type='DELEGATE_TASK', task='另一个公开问题')]), response.trace)
        return response
    model.react_call_for_session = changed
    # Exercise the capability boundary directly: a completed whole-turn replay
    # now returns its cognition receipt without buying two new model calls.
    from character_memory.runtime.reaction_engine import evaluate_reaction
    evaluation = evaluate_reaction(runtime, event=result.event, exclude_event_id=result.event.id)
    _, info = runtime.research_service.resolve(runtime, result.event, evaluation)
    assert len(runner.calls) == 1
    assert info['reason'] == 'REQUEST_ID_CONFLICT'
    assert 'REQUEST_ID_CONFLICT' in model.prompts[1]
    before = len(model.prompts)
    repeated = runtime.handle(result.event, persist_event=False)
    assert repeated.replayed and len(model.prompts) == before
    (tmp_path/'personas/echo/capabilities.yaml').write_text('capabilities: []\n', encoding='utf-8')
    assert runtime.research_service.capabilities(result.event) == ()
    _, denied = runtime.research_service.resolve(runtime, result.event, evaluation)
    assert denied['reason'] == 'ROLE_CAPABILITY_DENIED'
    assert len(runner.calls) == 1 and len(model.prompts) == before
    store.close()
