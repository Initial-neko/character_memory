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
