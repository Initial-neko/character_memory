"""Effective feedback: explicit choices outrank inference and candidates stay governed."""
from uuid import uuid4
import pytest

from test_message_receipts import acceptance, body
from character_memory.domain.experience_candidates import candidate_fields, interpret_feedback
from character_memory.storage.experience_profiles import ExperienceProfileRepository


def draft(client, store, group, scope='DIRECT'):
    from character_memory.application.reply_suggestion_signals import suggestion_service
    from character_memory.domain.reply_suggestions import SuggestionRequest
    from character_memory.domain.facts import FactRef
    from character_memory.domain.models import Event, EventType
    from character_memory.group_store import GroupRepository
    from test_reply_suggestion_signals import NOW, Model
    service = suggestion_service(store, client.app.state.character_memory)
    service.model_for = lambda: Model()
    version = ExperienceProfileRepository(store).get()['version']
    if scope == 'DIRECT':
        source = store.append_message(Event(character_id='momo', event_type=EventType.CHARACTER_MESSAGE,
                                            event_time=NOW, content='一起准备聚会吧'))
        ref = FactRef(kind='EVENT', id=source.id)
    else:
        from character_memory.domain.models import GroupEvent
        source = GroupRepository(store).append_event(GroupEvent(
            conversation_id=group.id, turn_id='t', actor_type='CHARACTER', actor_id='momo',
            event_type=EventType.CHARACTER_MESSAGE, event_time=NOW, content='一起准备聚会吧'))
        ref = FactRef(kind='CONVERSATION_EVENT', id=source.id, conversation_id=group.id)
    return service.generate(SuggestionRequest(request_id=uuid4(), context_ref=ref,
                                              expected_profile_version=version))


def send_batch(client, store, group, scope='DIRECT'):
    batch = draft(client, store, group, scope)
    payload = {**body(scope), 'message': batch['suggestions'][0]['text'],
               'reply_suggestion': {'batch_id': batch['batch_id'],
                                    'suggestion_id': batch['suggestions'][0]['id']}}
    sent = client.post('/v1/chat/messages' if scope == 'DIRECT' else f'/v1/groups/{group.id}/messages', json=payload)
    assert sent.status_code == 202, sent.text
    return batch


def feedback(client, batch, comment, *, changes=None, version=None):
    request = {'request_id': str(uuid4()), 'batch_id': batch['batch_id'],
               'kind': 'EXPLICIT_FEEDBACK', 'comment': comment}
    if changes is not None:
        request.update(preference_changes=changes, expected_profile_version=version)
    response = client.post('/v2/reply-suggestions/feedback', json=request)
    assert response.status_code == 200, response.text
    return response.json()


def candidates(client):
    response = client.get('/v2/me/experience-profile/candidates')
    assert response.status_code == 200, response.text
    return response.json()


def one_candidate(client, signal_id):
    return next(item for item in candidates(client)['candidates'] if item['signal_id'] == signal_id)


def apply_candidate(client, signal_id, version, *, as_long_term=False, request_id=None):
    return client.post(f'/v2/me/experience-profile/candidates/{signal_id}/apply', json={
        'request_id': request_id or str(uuid4()),
        'expected_profile_version': version,
        'as_long_term': as_long_term})


# --- interpretation boundary -------------------------------------------------

@pytest.mark.parametrize('text,fields,scope', [
    ('以后回答简短一点', {'verbosity': 'BRIEF'}, 'LONG_TERM'),
    ('这次别太可爱', {'tone': 'NATURAL'}, 'SCENE'),
    ('安静一点', {'energy': 'QUIET'}, 'SCENE'),
    ('以后别太长', {'verbosity': 'BRIEF'}, 'LONG_TERM'),
    ('详细一点，展开说说', {'verbosity': 'DETAILED'}, 'SCENE'),
])
def test_interpretation_is_bounded_and_explainable(text, fields, scope):
    interpretation = interpret_feedback(text)
    assert interpretation.fields == fields and interpretation.scope == scope
    assert interpretation.evidence and all(item.phrase for item in interpretation.evidence)


@pytest.mark.parametrize('text', [
    '正常聊就行',                      # no supported term
    '可爱又自然',                      # two same-length tone values: ambiguous
    '你今天想吃什么',                  # not feedback at all
    '   ',
])
def test_unsupported_or_ambiguous_text_yields_no_candidate(text):
    assert interpret_feedback(text) is None


def test_only_existing_schema_fields_can_be_proposed():
    interpretation = interpret_feedback('以后简短一点，也安静一点')
    assert set(interpretation.fields) <= {'tone', 'energy', 'verbosity'}
    with pytest.raises(Exception):
        type(interpretation)(scope='LONG_TERM', fields={'persona': 'overwrite'},
                             evidence=[], note='x')


def test_explicit_choice_outranks_interpretation():
    interpretation = interpret_feedback('以后简短一点，也安静一点')
    applicable, satisfied, overridden = candidate_fields(
        interpretation, {'verbosity': 'DETAILED', 'energy': 'QUIET'})
    assert applicable == {} and satisfied == ['energy'] and overridden == ['verbosity']
    applicable, _, _ = candidate_fields(interpretation, {'tone': 'CUTE'})
    assert applicable == {'energy': 'QUIET', 'verbosity': 'BRIEF'}


# --- real HTTP candidate lifecycle -------------------------------------------

def test_candidate_applies_reverts_and_keeps_history(acceptance):
    client, store, group, calls, _ = acceptance
    batch = send_batch(client, store, group)
    signal = feedback(client, batch, '以后回答简短一点')
    profile = ExperienceProfileRepository(store)

    listed = one_candidate(client, signal['id'])
    assert listed['status'] == 'PENDING' and listed['scope'] == 'LONG_TERM'
    assert listed['applicable_fields'] == {'verbosity': 'BRIEF'}
    assert '简短' in listed['evidence'][0]['phrase']

    applied = apply_candidate(client, signal['id'], 0)
    assert applied.status_code == 200, applied.text
    assert applied.json()['applied_profile_version'] == 1
    assert applied.json()['candidate']['status'] == 'APPLIED'
    assert profile.get()['preferences'] == {'verbosity': 'BRIEF'}
    assert one_candidate(client, signal['id'])['status'] == 'APPLIED'

    # Applying is a normal Profile CAS: stale expected_version cannot double write.
    stale = apply_candidate(client, signal['id'], 0)
    assert stale.status_code == 409
    assert profile.get()['version'] == 1

    reverted = client.post(f'/v2/me/experience-profile/candidates/{signal["id"]}/revert',
                           json={'request_id': str(uuid4()), 'expected_profile_version': 1})
    assert reverted.status_code == 200, reverted.text
    current = profile.get()
    assert current['version'] == 2 and current['preferences'] == {}
    history = {item['version']: item for item in profile.versions()}
    assert history[1]['preferences'] == {'verbosity': 'BRIEF'}      # history not rewritten
    assert history[2]['operation'] == 'PATCH' and history[2]['preferences'] == {}
    assert history[2]['previous_version'] == 1
    assert history[1]['operation'] == 'PATCH'                        # applied version kept as-is
    assert one_candidate(client, signal['id'])['status'] == 'REVERTED'
    assert client.post(f'/v2/me/experience-profile/candidates/{signal["id"]}/apply',
                       json={'request_id': str(uuid4()), 'expected_profile_version': 2}).status_code == 409


def test_scene_candidate_requires_long_term_confirmation(acceptance):
    client, store, group, calls, _ = acceptance
    batch = send_batch(client, store, group)
    signal = feedback(client, batch, '这次安静一点')
    assert one_candidate(client, signal['id'])['scope'] == 'SCENE'
    refused = apply_candidate(client, signal['id'], 0)
    assert refused.status_code == 409
    assert ExperienceProfileRepository(store).get()['version'] == 0
    applied = apply_candidate(client, signal['id'], 0, as_long_term=True)
    assert applied.status_code == 200 and applied.json()['applied_profile_version'] == 1
    assert ExperienceProfileRepository(store).get()['preferences'] == {'energy': 'QUIET'}


def test_explicit_choice_never_overwritten_by_inference(acceptance):
    client, store, group, calls, _ = acceptance
    profile = ExperienceProfileRepository(store)
    profile.patch(0, {'verbosity': 'DETAILED'})
    batch = send_batch(client, store, group)
    signal = feedback(client, batch, '以后简短一点')
    shown = one_candidate(client, signal['id'])
    assert shown['status'] == 'OVERRIDDEN'
    assert shown['applicable_fields'] == {} and shown['overridden_fields'] == ['verbosity']
    result = apply_candidate(client, signal['id'], 1)
    assert result.status_code == 200 and result.json()['applied_profile_version'] is None
    assert profile.get()['version'] == 1 and profile.get()['preferences'] == {'verbosity': 'DETAILED'}


def test_withdrawn_source_cannot_revive_candidate(acceptance):
    client, store, group, calls, _ = acceptance
    batch = send_batch(client, store, group)
    signal = feedback(client, batch, '以后简短一点')
    withdraw = client.post(f'/v2/reply-suggestions/feedback/{signal["id"]}/withdraw',
                           json={'request_id': str(uuid4()), 'expected_revision': 1})
    assert withdraw.status_code == 200, withdraw.text
    shown = one_candidate(client, signal['id'])
    assert shown['status'] == 'WITHDRAWN' and shown['source_withdrawn'] is True
    assert apply_candidate(client, signal['id'], 0).status_code == 409
    assert ExperienceProfileRepository(store).get()['version'] == 0


def test_cleared_profile_does_not_resurface_old_candidates_and_context(acceptance):
    client, store, group, calls, _ = acceptance
    batch = send_batch(client, store, group)
    signal = feedback(client, batch, '以后简短一点')
    assert apply_candidate(client, signal['id'], 0).json()['applied_profile_version'] == 1
    profile = ExperienceProfileRepository(store)
    assert profile.get()['preferences'] == {'verbosity': 'BRIEF'}

    cleared = client.delete('/v2/me/experience-profile', params={'expected_version': 1})
    assert cleared.status_code == 200 and cleared.json()['version'] == 2
    assert profile.get()['preferences'] == {}
    assert candidates(client)['candidates'][0]['status'] in {'APPLIED', 'SUPERSEDED'}
    assert apply_candidate(client, signal['id'], 2).status_code == 409
    assert profile.get()['version'] == 2


def test_clear_expires_open_candidates_so_old_value_does_not_reappear(acceptance):
    client, store, group, calls, _ = acceptance
    batch = send_batch(client, store, group)
    signal = feedback(client, batch, '以后简短一点')
    assert one_candidate(client, signal['id'])['status'] == 'PENDING'
    assert client.delete('/v2/me/experience-profile', params={'expected_version': 0}).status_code == 200
    assert one_candidate(client, signal['id'])['status'] == 'SUPERSEDED'
    assert apply_candidate(client, signal['id'], 1).status_code == 409
    assert ExperienceProfileRepository(store).get()['preferences'] == {}


def test_dismiss_marks_candidate_without_writing_profile(acceptance):
    client, store, group, calls, _ = acceptance
    batch = send_batch(client, store, group)
    signal = feedback(client, batch, '以后简短一点')
    dismissed = client.post(f'/v2/me/experience-profile/candidates/{signal["id"]}/dismiss',
                            json={'request_id': str(uuid4())})
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()['candidate']['status'] == 'DISMISSED'
    assert ExperienceProfileRepository(store).get()['version'] == 0
    assert apply_candidate(client, signal['id'], 0).status_code == 409


def test_apply_is_idempotent_by_request_identity(acceptance):
    client, store, group, calls, _ = acceptance
    batch = send_batch(client, store, group)
    signal = feedback(client, batch, '以后简短一点')
    request_id = str(uuid4())
    first = apply_candidate(client, signal['id'], 0, request_id=request_id)
    second = apply_candidate(client, signal['id'], 0, request_id=request_id)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()['applied_profile_version'] == 1
    assert ExperienceProfileRepository(store).get()['version'] == 1


def test_explicit_feedback_change_still_wins_in_the_same_submission(acceptance):
    client, store, group, calls, _ = acceptance
    batch = send_batch(client, store, group)
    # The visible dropdown in the same feedback sets tone explicitly.
    feedback(client, batch, '以后简短一点，可爱一点', changes={'tone': 'NATURAL'}, version=0)
    profile = ExperienceProfileRepository(store)
    assert profile.get()['preferences'] == {'tone': 'NATURAL'}
    listed = candidates(client)['candidates'][0]
    assert listed['fields']['tone'] == 'CUTE'
    assert listed['overridden_fields'] == ['tone']
    assert listed['applicable_fields'] == {'verbosity': 'BRIEF'}
    result = apply_candidate(client, listed['signal_id'], 1)
    assert result.status_code == 200
    assert profile.get()['preferences'] == {'tone': 'NATURAL', 'verbosity': 'BRIEF'}


@pytest.mark.parametrize('payload', [
    {'request_id': 'not-a-uuid', 'expected_profile_version': 0},
    {'request_id': str(uuid4()), 'expected_profile_version': True},
    {'request_id': str(uuid4()), 'expected_profile_version': 0, 'as_long_term': 'yes'},
    {'request_id': str(uuid4()), 'expected_profile_version': 0, 'extra': 1},
])
def test_apply_rejects_invalid_payload_without_writes(acceptance, payload):
    client, store, group, calls, _ = acceptance
    batch = send_batch(client, store, group)
    signal = feedback(client, batch, '以后简短一点')
    response = client.post(f'/v2/me/experience-profile/candidates/{signal["id"]}/apply', json=payload)
    assert response.status_code == 422
    assert ExperienceProfileRepository(store).get()['version'] == 0


def test_unknown_candidate_is_not_found(acceptance):
    client, store, group, calls, _ = acceptance
    assert client.post(f'/v2/me/experience-profile/candidates/{uuid4()}/apply',
                       json={'request_id': str(uuid4()), 'expected_profile_version': 0}).status_code == 404
    assert client.post(f'/v2/me/experience-profile/candidates/{uuid4()}/dismiss',
                       json={'request_id': str(uuid4())}).status_code == 404


# --- applied preference reaches the next real turn ---------------------------

def test_applied_candidate_reaches_next_direct_and_group_prompt(tmp_path):
    from test_reaction_receipts import CountedModel, runtime, group_service, NOW
    from character_memory.domain.models import Event, EventType
    from character_memory.storage.sqlite import SQLiteStore
    from character_memory.application.experience_candidates import FeedbackCandidateService
    from character_memory.storage.reply_suggestion_signals import ReplySuggestionSignalRepository
    from character_memory.domain.experience_candidates import CandidateApply

    class Recorder(CountedModel):
        def __init__(self):
            super().__init__()
            self.prompts = []
        def react_call_for_session(self, context, session_id):
            self.prompts.append(context)
            return super().react_call_for_session(context, session_id)

    store = SQLiteStore(tmp_path / 'growth.db')
    signals = ReplySuggestionSignalRepository(store)
    signal = signals.record(request_key='feedback:fixed', request_hash='hash', batch_id=str(uuid4()),
                            kind='EXPLICIT_FEEDBACK', profile_version=0, comment='以后回答简短一点')
    service = FeedbackCandidateService(store)
    assert service.list()['candidates'][0]['status'] == 'PENDING'
    result = service.apply(signal['id'], CandidateApply(request_id=str(uuid4()), expected_profile_version=0))
    assert result['applied_profile_version'] == 1

    model = Recorder()
    person = runtime(store, model)
    person.handle(Event(character_id='rin', event_type=EventType.USER_MESSAGE, event_time=NOW, content='hello'))
    assert 'verbosity=BRIEF' in model.prompts[-1]

    models = {'rin': Recorder(), 'momo': Recorder()}
    groups = group_service(store, models)
    group = groups.create_group('growth', ['rin', 'momo'], at=NOW)
    groups.react_from_event(groups.persist_user_event(group.id, 'hello', at=NOW))
    assert all('verbosity=BRIEF' in item.prompts[-1] for item in models.values())

    # Clearing removes the section from the next context in both channels.
    assert ExperienceProfileRepository(store).clear(1)['version'] == 2
    person.handle(Event(character_id='rin', event_type=EventType.USER_MESSAGE, event_time=NOW, content='after clear'))
    assert 'Explicit User Experience Preferences' not in model.prompts[-1]
    store.close()
