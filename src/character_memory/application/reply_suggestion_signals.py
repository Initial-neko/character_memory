"""Bind user-declared draft attribution to actual accepted messages atomically."""
import json
from character_memory.config import load_persona,resolve_persona_path
from character_memory.application.reply_suggestions import ReplySuggestionService,SuggestionStale
from character_memory.storage.reply_suggestion_signals import ReplySuggestionSignalRepository,fingerprint
from character_memory.domain.reply_suggestions import SuggestionRequest
from character_memory.message_projection import project_direct_message,project_group_message
from character_memory.group_store import GroupRepository


def suggestion_service(store,access):
    def ensure(character):
        profile=next((item for item in access.character_profiles() if item['id']==character),None)
        if profile is None:raise SuggestionStale('participant is unavailable')
        return profile
    def no_model():raise RuntimeError('signal recording must not initialize a model')
    from character_memory.storage.scenarios import ScenarioRepository
    return ReplySuggestionService(store,ensure_character=ensure,
        persona_for=lambda char:load_persona(resolve_persona_path(access.settings,char)),model_for=no_model,
        scenario_for=lambda activity_id,event_id:ScenarioRepository(store).suggestion_snapshot(activity_id,event_id))


def prepare_choice(store,access,attribution,scope):
    service=suggestion_service(store,access)
    batch=service.get(str(attribution.batch_id))
    if batch['status']!='READY':raise SuggestionStale('suggestions are not ready')
    ref=SuggestionRequest.model_validate_json(service.batches.get(batch['batch_id'])['request_json']).context_ref
    if scope[0]=='SCENARIO':
        # A practice draft may only be attached to the practice it was written
        # for; it can never be replayed onto a real Direct/Group message.
        matches=ref.kind=='SCENARIO_EVENT' and ref.activity_id==scope[1]
    elif scope[0]=='DIRECT':
        source=store.fact_reader.resolve_direct(ref) if ref.kind=='EVENT' else None
        matches=source is not None and source.character_id==scope[1]
    else:matches=ref.kind=='CONVERSATION_EVENT' and ref.conversation_id==scope[1]
    if not matches:raise SuggestionStale('suggestion batch belongs to another target')
    choice=None
    if attribution.suggestion_id is not None:
        choice=next((item for item in batch['suggestions'] if item['id']==str(attribution.suggestion_id)),None)
        if choice is None:raise SuggestionStale('suggestion is not in this batch')
    return batch,choice


def record_choice(store,prepared,source,scope,request_id):
    batch,choice=prepared
    if source.kind=='SCENARIO_EVENT':
        row=store.conn.execute(
            "SELECT * FROM scenario_events WHERE id=? AND activity_id=?",
            (source.id,source.activity_id)).fetchone()
        if row is None or row['actor_type']!='USER' or row['event_type']!='USER_MESSAGE':
            raise SuggestionStale('accepted user source is unavailable')
        text=str(row['content'] or '').strip()
    elif source.kind=='EVENT':
        event=store.fact_reader.resolve_direct(source, character_id=scope[1])
        if event is None or event.event_type.value!='USER_MESSAGE':
            raise SuggestionStale('accepted user source is unavailable')
        text=str(project_direct_message(event).get('content') or '').strip()
    else:
        row=store.conn.execute("SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
            "WHERE e.id=? AND e.conversation_id=? AND c.type='GROUP'",(source.id,source.conversation_id)).fetchone()
        if row is None or row['actor_type']!='USER' or row['actor_id']!='user':
            raise SuggestionStale('accepted user source is unavailable')
        text=str(project_group_message(GroupRepository._event_from_row(row),actor_name='user').get('content') or '').strip()
    kind='FREE_INPUT' if choice is None else ('SELECT' if text==choice['text'] else 'EDIT')
    return ReplySuggestionSignalRepository(store).record(
        request_key='message:'+json.dumps(scope,ensure_ascii=False)+':'+request_id,
        request_hash=fingerprint({'source':source.model_dump(),'batch':batch['batch_id'],'suggestion':choice['id'] if choice else None}),
        batch_id=batch['batch_id'],suggestion_id=choice['id'] if choice else None,
        kind=kind,source_ref=source,profile_version=batch['profile_version'])


def record_feedback(service,request):
    store=service.store;repo=ReplySuggestionSignalRepository(store)
    key='feedback:'+str(request.request_id);digest=fingerprint(request.model_dump(mode='json'))
    with store.transaction(immediate=True):
        existing=repo.existing(key,digest)
        if existing is not None:return existing
        historical=(request.kind=='EXPLICIT_FEEDBACK' and repo.has_accepted_source(str(request.batch_id)))
        if historical:
            # This local author's accepted source is provenance, not a fresh
            # grant to read or revive the old conversation. Only explicit global
            # Profile fields may change, guarded by the CURRENT Profile CAS.
            row=service.batches.get(str(request.batch_id))
            if row is None:raise KeyError(str(request.batch_id))
            original=SuggestionRequest.model_validate_json(row['request_json'])
            batch={'batch_id':row['id'],'profile_version':original.expected_profile_version}
        else:
            batch=service.get(str(request.batch_id))
            if batch['status']!='READY':raise SuggestionStale('suggestions are not ready')

        applied=None
        if request.preference_changes is not None:
            applied=service.profiles.patch(request.expected_profile_version,request.preference_changes.model_dump(exclude_unset=True))['version']
        return repo.record(request_key=key,request_hash=digest,batch_id=batch['batch_id'],kind=request.kind,
                           profile_version=batch['profile_version'],applied_profile_version=applied,comment=request.comment)
