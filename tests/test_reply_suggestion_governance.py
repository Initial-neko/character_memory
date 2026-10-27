"""Author correction/withdrawal never rewrites messages or resurrects old notes."""
from uuid import uuid4
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from test_message_receipts import acceptance,body,endpoint
from test_reply_suggestion_signals import batch_for


def feedback(client,store,group,comment='PRIVATE_INITIAL_NOTE',changes=False):
    batch=batch_for(client,store,group,'DIRECT')
    payload={'request_id':str(uuid4()),'batch_id':batch['batch_id'],'kind':'EXPLICIT_FEEDBACK','comment':comment}
    if changes:payload.update(preference_changes={'tone':'CUTE'},expected_profile_version=0)
    result=client.post('/v2/reply-suggestions/feedback',json=payload);assert result.status_code==200,result.text
    return batch,payload,result.json()


def mutation(version=1,**kwargs):return {'request_id':str(uuid4()),'expected_revision':version,**kwargs}


def test_correction_has_cas_idempotency_and_no_old_note_snapshot(acceptance):
    client,store,group,calls,_=acceptance;batch,original,signal=feedback(client,store,group)
    change=mutation(comment='PRIVATE_CORRECTED_NOTE');url='/v2/reply-suggestions/feedback/'+signal['id']
    result=client.patch(url,json=change);assert result.status_code==200,result.text
    assert result.json()['revision']==2 and 'comment' not in result.json()
    assert client.patch(url,json=change).json()==result.json()
    rows=client.get('/v2/reply-suggestions/'+batch['batch_id']+'/signals').json()['signals']
    assert rows[0]['revision']==2 and rows[0]['comment']=='PRIVATE_CORRECTED_NOTE'
    assert client.post('/v2/reply-suggestions/feedback',json=original).json()['comment']=='PRIVATE_CORRECTED_NOTE'
    assert client.patch(url,json={**change,'comment':'different'}).status_code==409
    assert 'PRIVATE_INITIAL_NOTE' not in '\n'.join(store.conn.iterdump())
    assert len(calls)==0


def test_withdrawal_clears_note_and_replays_do_not_restore_preferences(acceptance):
    from character_memory.storage.experience_profiles import ExperienceProfileRepository
    client,store,group,calls,_=acceptance;batch,original,signal=feedback(client,store,group,changes=True)
    path='/v2/reply-suggestions/feedback/'+signal['id'];withdraw=mutation()
    applied=client.post(path+'/withdraw',json=withdraw);assert applied.status_code==200,applied.text
    assert applied.json()['revision']==2
    assert client.post(path+'/withdraw',json=withdraw).json()==applied.json()
    current=client.get('/v2/reply-suggestions/'+batch['batch_id']+'/signals').json()['signals'][0]
    assert current['withdrawn'] is True and current['comment'] is None
    assert client.get('/v2/reply-suggestions/'+batch['batch_id']+'/signals?active_only=true').json()['signals']==[]
    profile=ExperienceProfileRepository(store);assert profile.get()['preferences']=={'tone':'CUTE'}
    profile.clear(1)
    replay=client.post('/v2/reply-suggestions/feedback',json=original).json()
    assert replay['withdrawn'] is True and replay['comment'] is None and profile.get()['preferences']=={}
    assert client.patch(path,json=mutation(2,comment='must not resurrect')).status_code==409
    assert 'PRIVATE_INITIAL_NOTE' not in '\n'.join(store.conn.iterdump())
    assert len(calls)==0


@pytest.mark.parametrize('scope',['DIRECT','GROUP'])
def test_withdrawn_choice_keeps_fact_and_acceptance_replay_shows_current_state(acceptance,scope):
    client,store,group,calls,_=acceptance;batch=batch_for(client,store,group,scope)
    payload={**body(scope),'message':batch['suggestions'][0]['text'],'reply_suggestion':{'batch_id':batch['batch_id'],'suggestion_id':batch['suggestions'][0]['id']}}
    first=client.post(endpoint(group,scope),json=payload);assert first.status_code==202
    signal=first.json()['reply_suggestion_signal'];source=signal['source_ref']
    result=client.post('/v2/reply-suggestions/feedback/'+signal['id']+'/withdraw',json=mutation());assert result.status_code==200,result.text
    replay=client.post(endpoint(group,scope),json=payload);assert replay.status_code==202,replay.text
    assert replay.json()['event_id']==source['id'] and replay.json()['reply_suggestion_signal']['source_ref']==source
    assert replay.json()['reply_suggestion_signal']['withdrawn'] is True
    assert replay.json()['reply_suggestion_signal']['kind']=='SELECT' and len(calls)==1


@pytest.mark.parametrize('changes',[{'expected_revision':True},{'expected_revision':0},{'comment':'  '},{'owner_id':'other'},{'kind':'SELECT'}])
def test_mutation_fields_are_strict_and_source_identity_not_editable(acceptance,changes):
    client,store,group,calls,_=acceptance;batch,original,signal=feedback(client,store,group)
    response=client.patch('/v2/reply-suggestions/feedback/'+signal['id'],json={**mutation(comment='new'),**changes})
    assert response.status_code==422
    assert client.get('/v2/reply-suggestions/'+batch['batch_id']+'/signals').json()['signals'][0]['comment']=='PRIVATE_INITIAL_NOTE'


def test_stale_revision_and_unknown_signal_are_distinct(acceptance):
    client,store,group,calls,_=acceptance;batch,original,signal=feedback(client,store,group)
    assert client.patch('/v2/reply-suggestions/feedback/'+signal['id'],json=mutation(2,comment='stale')).status_code==409
    assert client.patch('/v2/reply-suggestions/feedback/'+str(uuid4()),json=mutation(comment='missing')).status_code==404


def test_signal_change_and_metadata_receipt_roll_back_together(acceptance):
    from character_memory.storage.reply_suggestion_signals import ReplySuggestionSignalRepository
    from character_memory.domain.reply_suggestions import FeedbackCorrection
    client,store,group,calls,_=acceptance;batch,original,signal=feedback(client,store,group);repo=ReplySuggestionSignalRepository(store)
    store.conn.execute("CREATE TRIGGER reject_signal_change BEFORE INSERT ON reply_suggestion_signal_changes BEGIN SELECT RAISE(ABORT,'receipt rejected'); END");store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):repo.change(signal['id'],'CORRECT',FeedbackCorrection(**mutation(comment='rolled back')))
    current=repo.get(signal['id']);assert current['revision']==1 and current['comment']=='PRIVATE_INITIAL_NOTE'
    assert store.conn.execute('SELECT COUNT(*) FROM reply_suggestion_signal_changes').fetchone()[0]==0


def test_same_change_identity_cannot_mutate_another_signal(acceptance):
    client,store,group,calls,_=acceptance;batch,original,first=feedback(client,store,group)
    _,_,second=feedback(client,store,group,comment='SECOND_NOTE');change=mutation(comment='first changed')
    assert client.patch('/v2/reply-suggestions/feedback/'+first['id'],json=change).status_code==200
    assert client.patch('/v2/reply-suggestions/feedback/'+second['id'],json=change).status_code==409


def test_two_connections_only_one_governance_revision_wins(tmp_path):
    from character_memory.storage.sqlite import SQLiteStore
    from character_memory.storage.reply_suggestion_signals import ReplySuggestionSignalRepository
    from character_memory.domain.reply_suggestions import FeedbackCorrection
    from character_memory.storage.reply_suggestions import SuggestionConflict
    stores=[SQLiteStore(tmp_path/'governance.db'),SQLiteStore(tmp_path/'governance.db')];repos=[ReplySuggestionSignalRepository(s) for s in stores]
    signal=repos[0].record(request_key='feedback:one',request_hash='hash',batch_id=str(uuid4()),kind='EXPLICIT_FEEDBACK',profile_version=0,comment='original')
    gate=threading.Barrier(2)
    def update(index):
        gate.wait()
        try:return repos[index].change(signal['id'],'CORRECT',FeedbackCorrection(**mutation(comment='choice '+str(index))))['revision']
        except SuggestionConflict:return 'CONFLICT'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(update,[0,1]))
    assert sorted(map(str,results))==['2','CONFLICT']
    assert stores[0].conn.execute('SELECT COUNT(*) FROM reply_suggestion_signal_changes').fetchone()[0]==1
    for store in stores:store.close()



def test_original_correction_retry_after_withdraw_cannot_recover_comment(acceptance):
    client,store,group,calls,_=acceptance;batch,original,signal=feedback(client,store,group)
    path='/v2/reply-suggestions/feedback/'+signal['id'];change=mutation(comment='PRIVATE_EDITED_NOTE')
    corrected=client.patch(path,json=change);assert corrected.status_code==200
    assert client.post(path+'/withdraw',json=mutation(2)).status_code==200
    assert client.patch(path,json=change).json()==corrected.json()
    row=client.get('/v2/reply-suggestions/'+batch['batch_id']+'/signals').json()['signals'][0]
    assert row['revision']==3 and row['withdrawn'] is True and row['comment'] is None
    dump='\n'.join(store.conn.iterdump())
    assert 'PRIVATE_INITIAL_NOTE' not in dump and 'PRIVATE_EDITED_NOTE' not in dump


def test_correction_cannot_reclassify_a_real_selected_message(acceptance):
    client,store,group,calls,_=acceptance;batch=batch_for(client,store,group,'DIRECT')
    sent=client.post(endpoint(group,'DIRECT'),json={**body('DIRECT'),'message':batch['suggestions'][0]['text'],'reply_suggestion':{'batch_id':batch['batch_id'],'suggestion_id':batch['suggestions'][0]['id']}})
    signal=sent.json()['reply_suggestion_signal'];assert signal['kind']=='SELECT'
    assert client.patch('/v2/reply-suggestions/feedback/'+signal['id'],json=mutation(comment='not an authored note')).status_code==409
    assert len(calls)==1


def test_trusted_owner_boundary_is_not_overridden_by_known_signal_id(acceptance):
    client,store,group,calls,_=acceptance;batch,original,signal=feedback(client,store,group)
    store.conn.execute("UPDATE reply_suggestion_signals SET owner_id='another-trusted-owner' WHERE id=?",(signal['id'],));store.conn.commit()
    assert client.patch('/v2/reply-suggestions/feedback/'+signal['id'],json=mutation(comment='must not change')).status_code==404
    assert store.conn.execute('SELECT comment FROM reply_suggestion_signals WHERE id=?',(signal['id'],)).fetchone()[0]=='PRIVATE_INITIAL_NOTE'


def test_existing_signal_schema_migrates_once_without_losing_authored_note(tmp_path):
    from character_memory.storage.sqlite import SQLiteStore
    from character_memory.storage.reply_suggestion_signals import ReplySuggestionSignalRepository
    from character_memory.domain.reply_suggestions import FeedbackCorrection
    store=SQLiteStore(tmp_path/'legacy-signals.db');identifier=str(uuid4())
    def old_schema():
        store.conn.execute('CREATE TABLE reply_suggestion_signals(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,request_key TEXT NOT NULL,request_hash TEXT NOT NULL,batch_id TEXT NOT NULL,suggestion_id TEXT,kind TEXT NOT NULL,source_ref_json TEXT,comment TEXT,profile_version INTEGER NOT NULL,applied_profile_version INTEGER,created_at TEXT NOT NULL,UNIQUE(owner_id,request_key),UNIQUE(owner_id,source_ref_json))')
    store.apply_schema_migration('experience/003-suggestion-signals',old_schema,immediate=True)
    store.conn.execute('INSERT INTO reply_suggestion_signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,'local-operator','old-request','old-hash',str(uuid4()),None,'EXPLICIT_FEEDBACK',None,'old authored note',0,None,'2026-10-26T00:00:00+00:00'));store.conn.commit()
    repo=ReplySuggestionSignalRepository(store);row=repo.get(identifier)
    assert row['comment']=='old authored note' and row['revision']==1 and row['withdrawn'] is False
    repo.change(identifier,'CORRECT',FeedbackCorrection(**mutation(comment='new authored note')))
    assert ReplySuggestionSignalRepository(store).get(identifier)['revision']==2
    assert store.conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='experience/004-signal-governance'").fetchone()[0]==1
    store.close()
