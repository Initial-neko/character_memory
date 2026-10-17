import pytest
from character_memory.character_life import CharacterLifeReader
from test_world_continuation import setup
from test_world_activity import NOW


def world_item(store):
    return next(i for i in CharacterLifeReader(store).timeline('c00', channel='WORLD')['items'] if i['kind'] == 'world')


def test_life_stop_is_completed_silence_not_pending(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [{'action':'STOP'}])
    service.browse_character('c00', now=NOW, opportunity_id='life-stop')
    before=store.conn.total_changes
    item=world_item(store)
    assert item['status']=='NO_ACTION'
    assert item['continuation']['steps']==[]
    assert item['continuation']['stop_reason']=='MODEL_STOP'
    assert store.conn.total_changes==before
    assert '决策记录尚未完成' not in item['content']
    store.close()


def test_life_success_exposes_actual_steps_and_sources_without_model_prompt(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [
        {'action':'CALL','capability':'WEB_SEARCH','arguments':{'query':'agent memory'}},
        {'action':'STOP','keep':True,'summary':'Actual saved summary','personal_note':'My saved note'}])
    result=service.browse_character('c00', now=NOW, opportunity_id='life-read')
    before=store.conn.total_changes
    item=world_item(store)
    assert item['status']=='SUCCESS'
    assert item['continuation']['steps'][0]['request_id']==result['steps'][0]['request_id']
    assert item['continuation']['sources'][0]['url']=='https://example.com/agent-memory'
    assert result['steps'][0]['request_id'] in item['content']
    assert 'https://example.com/agent-memory' in item['content']
    assert 'Actual saved summary' in item['content']
    assert CharacterLifeReader(store).timeline('c01', channel='WORLD')['items']==[]
    assert CharacterLifeReader(store).timeline('c00',query='Actual saved summary')['items']
    assert store.conn.total_changes==before
    store.close()


def test_life_denied_step_is_not_successful_activity(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [
        {'action':'CALL','capability':'WEB_SEARCH','arguments':{'query':'x','character_id':'c01'}},
        {'action':'STOP'}])
    service.browse_character('c00',now=NOW,opportunity_id='life-denied')
    item=world_item(store)
    assert item['status']=='DENIED'
    assert item['continuation']['sources']==[]
    assert item['continuation']['steps'][0]['status']=='DENIED'
    assert not store.list_events('c00')
    store.close()



def test_life_all_three_receipts_remain_inspectable(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [
        {'action':'CALL','capability':'WEB_SEARCH','arguments':{'query':f'public {i}'}} for i in range(3)])
    result=service.browse_character('c00',now=NOW,opportunity_id='life-multiple')
    item=world_item(store)
    assert len(item['continuation']['steps'])==3
    assert len(item['continuation']['sources'])==3
    assert all(step['request_id'] in item['content'] for step in result['steps'])
    assert item['continuation']['stop_reason']=='STEP_BUDGET_EXHAUSTED'
    store.close()


@pytest.mark.parametrize('phase', ['CONT_MODEL:1','CONT_EXECUTING:1'])
def test_life_incomplete_decision_does_not_claim_success(tmp_path,phase):
    store, access, model, prompts, service = setup(tmp_path, [])
    service.repository.claim_browse_decision('life-incomplete','c00',NOW)
    plan={'mode':'CONTINUATION','steps':[]}
    if phase.startswith('CONT_EXECUTING'):
        plan['pending_step']={'character_id':'c00','opportunity_id':'life-incomplete','request_id':'life-incomplete:CONT:1','capability':'BROWSER_OPEN','arguments':{'url':'https://example.com/not-confirmed'}}
    service.repository.update_browse_decision('life-incomplete',expected_phase='PLANNING',phase=phase,plan=plan)
    item=world_item(store)
    assert item['status']==('UNKNOWN' if phase.startswith('CONT_EXECUTING') else 'PENDING')
    assert item['continuation']['sources']==[]
    assert 'https://example.com/not-confirmed' not in item['content']
    assert not prompts
    store.close()


def test_life_unknown_failure_stays_unknown_without_retry(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [])
    service.repository.claim_browse_decision('life-unknown','c00',NOW)
    service.repository.update_browse_decision('life-unknown',expected_phase='PLANNING',phase='FAILED',plan={'mode':'CONTINUATION','steps':[]},result={'continuation':True,'execution_status':'UNKNOWN','steps':[],'stop_reason':'TimeoutError'})
    before=store.conn.total_changes
    item=world_item(store)
    assert item['status']=='UNKNOWN'
    assert item['continuation']['sources']==[]
    assert not prompts and store.conn.total_changes==before
    store.close()


def test_life_does_not_expose_hidden_prompt_arguments_or_other_person_steps(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [])
    service.repository.claim_browse_decision('life-invalid','c00',NOW)
    service.repository.update_browse_decision('life-invalid',expected_phase='PLANNING',phase='APPLIED',plan={'mode':'CONTINUATION','persona':'private-canary','decisions':[{'prompt':'private-canary'}]},result={'continuation':True,'execution_status':'SUCCESS','steps':[{'character_id':'c01','opportunity_id':'life-invalid','capability':'BROWSER_OPEN','status':'SUCCESS','request_id':'other-private-request','arguments':{'query':'private-canary'},'data':{'observation':{'final_url':'https://example.com/private-canary','text':'Not mine'}}}]})
    item=world_item(store)
    assert item['status']=='UNKNOWN' and item['continuation']['steps']==[]
    assert 'private-canary' not in str(item)
    assert 'other-private-request' not in str(item)
    assert not CharacterLifeReader(store).timeline('c00',query='private-canary')['items']
    store.close()
