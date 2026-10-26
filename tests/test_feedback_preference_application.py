"""Explicit post-send preference changes retain CAS and historical fact boundaries."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import threading

import pytest
from test_message_receipts import acceptance, body, endpoint
from test_reply_suggestion_signals import batch_for
from character_memory.application.reply_suggestion_signals import record_feedback, suggestion_service
from character_memory.application.reply_suggestions import SuggestionStale
from character_memory.domain.reply_suggestions import SuggestionFeedback
from character_memory.storage.experience_profiles import ExperienceProfileRepository, ProfileConflict
from character_memory.storage.reply_suggestion_signals import ReplySuggestionSignalRepository


def accepted_batch(acceptance, scope='DIRECT'):
    client, store, group, calls, _ = acceptance
    batch = batch_for(client, store, group, scope)
    choice = batch['suggestions'][0]
    payload = {**body(scope), 'message': choice['text'], 'reply_suggestion': {
        'batch_id': batch['batch_id'], 'suggestion_id': choice['id']}}
    sent = client.post(endpoint(group, scope), json=payload)
    assert sent.status_code == 202, sent.text
    return batch, payload, sent.json()['reply_suggestion_signal']


def feedback(batch, version=0, **changes):
    return {'request_id': str(uuid4()), 'batch_id': batch['batch_id'],
            'kind': 'EXPLICIT_FEEDBACK', 'comment': '明确选择以后简短一点',
            'expected_profile_version': version,
            'preference_changes': changes or {'verbosity': 'BRIEF'}}


@pytest.mark.parametrize('scope', ['DIRECT', 'GROUP'])
def test_post_send_feedback_uses_current_profile_cas_without_reviving_batch(acceptance, scope):
    client, store, group, calls, _ = acceptance
    batch, payload, source = accepted_batch(acceptance, scope)
    profile = ExperienceProfileRepository(store)
    profile.patch(0, {'tone': 'CUTE'})
    result = client.post('/v2/reply-suggestions/feedback', json=feedback(batch, 1))
    assert result.status_code == 200, result.text
    signal = result.json()
    assert signal['profile_version'] == 0
    assert signal['applied_profile_version'] == 2
    assert profile.get()['preferences'] == {'tone': 'CUTE', 'verbosity': 'BRIEF'}
    assert client.get('/v2/reply-suggestions/' + batch['batch_id']).status_code == 409
    assert client.post('/v2/reply-suggestions/feedback', json={
        'request_id': str(uuid4()), 'batch_id': batch['batch_id'], 'kind': 'IGNORE'}).status_code == 409
    assert client.post(endpoint(group, scope), json={**payload, 'request_id': 'another-send'}).status_code == 409
    assert len(calls) == 1
    assert source['source_ref'] is not None


@pytest.mark.parametrize('reason', ['unaccepted', 'other-batch-owner', 'other-source-owner', 'unknown'])
def test_historical_preference_requires_own_accepted_batch(acceptance, reason):
    client, store, group, calls, _ = acceptance
    if reason == 'unaccepted':
        batch = batch_for(client, store, group, 'DIRECT')
    else:
        batch, _, source = accepted_batch(acceptance)
    profile = ExperienceProfileRepository(store)
    profile.patch(0, {'tone': 'NATURAL'})
    if reason == 'other-batch-owner':
        store.conn.execute("UPDATE reply_suggestion_batches SET owner_id='other' WHERE id=?", (batch['batch_id'],))
    elif reason == 'other-source-owner':
        store.conn.execute("UPDATE reply_suggestion_signals SET owner_id='other' WHERE id=?", (source['id'],))
    elif reason == 'unknown':
        batch = {**batch, 'batch_id': str(uuid4())}
    store.conn.commit()
    response = client.post('/v2/reply-suggestions/feedback', json=feedback(batch, 1))
    assert response.status_code == (404 if reason in {'other-batch-owner', 'unknown'} else 409), response.text
    assert profile.get()['version'] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM reply_suggestion_signals WHERE kind='EXPLICIT_FEEDBACK'").fetchone()[0] == 0


@pytest.mark.parametrize('group_change', ['archived', 'left', 'withdrawn-source'])
def test_historical_application_never_reads_group_context_or_calls_model(acceptance, monkeypatch, group_change):
    client, store, group, calls, _ = acceptance
    batch, _, source = accepted_batch(acceptance, 'GROUP')
    if group_change == 'archived':
        store.conn.execute("UPDATE conversations SET archived_at='2026-10-26T00:00:00Z' WHERE id=?", (group.id,))
    elif group_change == 'left':
        store.conn.execute("DELETE FROM conversation_members WHERE conversation_id=? AND actor_type='USER' AND actor_id='user'", (group.id,))
    else:
        response = client.post('/v2/reply-suggestions/feedback/' + source['id'] + '/withdraw', json={
            'request_id': str(uuid4()), 'expected_revision': 1})
        assert response.status_code == 200
    store.conn.commit()
    service = suggestion_service(store, client.app.state.character_memory)
    def forbidden(*args, **kwargs):
        raise AssertionError('historical preference must not inspect old context or initialize model')
    for name in ('_snapshot', 'ensure_character', 'model_for'):
        monkeypatch.setattr(service, name, forbidden)
    before = store.conn.execute('SELECT COUNT(*) FROM conversation_events').fetchone()[0]
    result = record_feedback(service, SuggestionFeedback(**feedback(batch)))
    assert result['applied_profile_version'] == 1
    assert ExperienceProfileRepository(store).get()['preferences'] == {'verbosity': 'BRIEF'}
    assert store.conn.execute('SELECT COUNT(*) FROM conversation_events').fetchone()[0] == before
    assert len(calls) == 1


def test_historical_feedback_and_profile_roll_back_together(acceptance, monkeypatch):
    client, store, group, calls, _ = acceptance
    batch, _, _ = accepted_batch(acceptance)
    def fail(*args, **kwargs):
        raise RuntimeError('injected signal failure')
    monkeypatch.setattr(ReplySuggestionSignalRepository, 'record', fail)
    with pytest.raises(RuntimeError, match='injected signal failure'):
        client.post('/v2/reply-suggestions/feedback', json=feedback(batch))
    assert ExperienceProfileRepository(store).get()['version'] == 0
    assert store.conn.execute('SELECT COUNT(*) FROM experience_profile_versions').fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM reply_suggestion_signals WHERE kind='EXPLICIT_FEEDBACK'").fetchone()[0] == 0


def test_historical_feedback_race_has_one_cas_winner(acceptance):
    from character_memory.storage.sqlite import SQLiteStore
    client, store, group, calls, _ = acceptance
    batch, _, _ = accepted_batch(acceptance)
    path = store.conn.execute('PRAGMA database_list').fetchone()[2]
    other = SQLiteStore(path)
    services = [suggestion_service(item, client.app.state.character_memory) for item in (store, other)]
    gate = threading.Barrier(2)
    def apply(index):
        gate.wait()
        try:
            return record_feedback(services[index], SuggestionFeedback(**feedback(batch, tone=['CUTE', 'NATURAL'][index])))['applied_profile_version']
        except (ProfileConflict, SuggestionStale):
            return 'CONFLICT'
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(apply, [0, 1]))
        assert sorted(map(str, results)) == ['1', 'CONFLICT']
        assert store.conn.execute("SELECT COUNT(*) FROM reply_suggestion_signals WHERE kind='EXPLICIT_FEEDBACK'").fetchone()[0] == 1
    finally:
        other.close()


def test_old_feedback_replay_never_reapplies_after_later_profile_operations(acceptance):
    client, store, group, calls, _ = acceptance
    batch, _, _ = accepted_batch(acceptance)
    original = feedback(batch)
    first = client.post('/v2/reply-suggestions/feedback', json=original)
    assert first.status_code == 200, first.text
    profile = ExperienceProfileRepository(store)
    assert client.post('/v2/reply-suggestions/feedback', json=original).json() == first.json()
    profile.patch(1, {'verbosity': 'DETAILED'})
    profile.restore(2, 1)
    profile.clear(3)
    profile.patch(4, {'tone': 'NATURAL'})
    current = profile.get()
    replay = client.post('/v2/reply-suggestions/feedback', json=original)
    assert replay.status_code == 200 and replay.json()['applied_profile_version'] == 1
    assert profile.get() == current
    assert client.post('/v2/reply-suggestions/feedback', json={**original, 'comment': 'changed'}).status_code == 409
    assert store.conn.execute("SELECT COUNT(*) FROM reply_suggestion_signals WHERE kind='EXPLICIT_FEEDBACK'").fetchone()[0] == 1


def test_text_governance_does_not_silently_undo_applied_preferences(acceptance):
    client, store, group, calls, _ = acceptance
    batch, _, _ = accepted_batch(acceptance)
    original = feedback(batch)
    result = client.post('/v2/reply-suggestions/feedback', json=original)
    assert result.status_code == 200, result.text
    url = '/v2/reply-suggestions/feedback/' + result.json()['id']
    assert client.patch(url, json={'request_id': str(uuid4()), 'expected_revision': 1, 'comment': '更正文字'}).status_code == 200
    assert client.post(url + '/withdraw', json={'request_id': str(uuid4()), 'expected_revision': 2}).status_code == 200
    assert ExperienceProfileRepository(store).get()['preferences'] == {'verbosity': 'BRIEF'}
    replay = client.post('/v2/reply-suggestions/feedback', json=original).json()
    assert replay['withdrawn'] is True and replay['comment'] is None
    assert ExperienceProfileRepository(store).get()['version'] == 1
