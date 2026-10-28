"""Person goal stages are explicit acceptances with live, scoped fact evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import sqlite3
import threading
from uuid import uuid4

import pytest
from pydantic import ValidationError

from character_memory.domain.facts import FactRef
from character_memory.domain.goals import GoalCreate, GoalAction
from character_memory.domain.models import Event, EventType
from character_memory.group_store import GroupRepository, GroupEvent
from character_memory.storage.person_goals import GoalRepository, GoalMissing, GoalConflict, EvidenceUnavailable
from character_memory.storage.sqlite import SQLiteStore

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
SIM = {'schema_version': 1, 'reality': 'SIMULATED', 'activity_id': 'practice', 'branch_id': 'branch'}


def create_request(**changes):
    data = dict(request_id=str(uuid4()), title='整理本周观察', success_criteria='明确接受两个可追溯阶段', stages=['观察一项真实来源', '整理观察并复核'])
    data.update(changes)
    return GoalCreate(**data)


def action(version, operation, **changes):
    data = dict(request_id=str(uuid4()), expected_version=version, operation=operation)
    data.update(changes)
    return GoalAction(**data)


def source(store, character='rin', content='PRIVATE_SOURCE_BODY', **metadata):
    event = store.append_message(Event(character_id=character, event_type=EventType.USER_MESSAGE,
        event_time=NOW, content=content, metadata=metadata))
    return FactRef(kind='EVENT', id=event.id)


def complete(version, refs, **changes):
    return action(version, 'COMPLETE_STAGE', confirmation='OPERATOR_ACCEPTED', evidence=refs, **changes)


@pytest.fixture
def state(tmp_path):
    store = SQLiteStore(tmp_path / 'goals.sqlite')
    repo = GoalRepository(store)
    yield store, repo
    store.close()


def test_ordered_stages_pause_resume_complete_and_archive(state):
    store, repo = state
    created = repo.create('rin', create_request())
    goal = created['goal']; gid = goal['id']
    assert (goal['phase'], goal['current_stage'], goal['version']) == ('ACTIVE', 0, 1)
    assert not created['replayed'] and created['receipt']['version_applied'] == 1
    assert repo.act('rin', gid, action(1, 'PAUSE'))['goal']['phase'] == 'PAUSED'
    with pytest.raises(GoalConflict):
        repo.act('rin', gid, complete(2, [source(store)]))
    assert repo.act('rin', gid, action(2, 'RESUME'))['goal']['version'] == 3
    ref = source(store)
    first = repo.act('rin', gid, complete(3, [ref], note='操作者接受第一阶段'))['goal']
    assert (first['phase'], first['current_stage'], first['version']) == ('ACTIVE', 1, 4)
    final = repo.act('rin', gid, complete(4, [ref]))['goal']
    assert (final['phase'], final['current_stage'], final['version']) == ('COMPLETED', 2, 5)
    assert [item['stage_index'] for item in final['completions']] == [0, 1]
    assert all(item['confirmation'] == 'OPERATOR_ACCEPTED' and item['accepted_at'] for item in final['completions'])
    assert repo.act('rin', gid, action(5, 'ARCHIVE'))['goal']['phase'] == 'ARCHIVED'
    with pytest.raises(GoalConflict):
        repo.act('rin', gid, action(6, 'RESUME'))
    assert len(repo.list('rin')) == 1 and repo.list('other') == []


def test_request_receipts_survive_archive_and_never_reapply(state):
    store, repo = state
    request = create_request()
    original = repo.create('rin', request)
    gid = original['goal']['id']
    accepted = complete(1, [source(store)])
    acknowledgment = repo.act('rin', gid, accepted)
    repo.act('rin', gid, action(2, 'ARCHIVE'))
    create_replay = repo.create('rin', request)
    action_replay = repo.act('rin', gid, accepted)
    for replay in [create_replay, action_replay]:
        assert replay['replayed'] and replay['goal']['phase'] == 'ARCHIVED'
        assert replay['goal']['version'] == 3
    assert create_replay['receipt'] == original['receipt']
    assert action_replay['receipt'] == acknowledgment['receipt']
    assert len(action_replay['goal']['completions']) == 1
    with pytest.raises(GoalConflict):
        repo.create('rin', create_request(request_id=request.request_id, title='changed'))
    with pytest.raises(GoalConflict):
        repo.act('rin', gid, action(1, 'PAUSE', request_id=accepted.request_id))


def test_request_identity_cannot_be_rebound_to_another_goal(state):
    store, repo = state
    first = repo.create('rin', create_request())['goal']
    second = repo.create('rin', create_request())['goal']
    pause = action(1, 'PAUSE')
    repo.act('rin', first['id'], pause)
    with pytest.raises(GoalConflict):
        repo.act('rin', second['id'], pause)
    with pytest.raises(GoalMissing):
        repo.get('other', first['id'])
    with pytest.raises(GoalMissing):
        repo.act('other', first['id'], action(1, 'PAUSE'))
    with pytest.raises(GoalMissing):
        repo.get('rin', str(uuid4()))


def test_same_version_two_connections_only_one_stage_acceptance(tmp_path):
    path = tmp_path / 'race.sqlite'
    stores = [SQLiteStore(path), SQLiteStore(path)]
    repos = [GoalRepository(item) for item in stores]
    gid = repos[0].create('rin', create_request())['goal']['id']
    ref = source(stores[0])
    gate = threading.Barrier(2)
    def apply(index):
        gate.wait()
        try:
            return repos[index].act('rin', gid, complete(1, [ref]))['goal']['version']
        except GoalConflict:
            return 'CONFLICT'
    try:
        with ThreadPoolExecutor(2) as pool:
            assert sorted(map(str, pool.map(apply, [0, 1]))) == ['2', 'CONFLICT']
        goal = repos[0].get('rin', gid)
        assert goal['current_stage'] == 1 and len(goal['completions']) == 1
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize('reason', ['wrong-character', 'missing', 'simulated', 'bad-scope'])
def test_stage_evidence_is_same_person_real_and_fail_closed(state, reason):
    store, repo = state
    gid = repo.create('rin', create_request())['goal']['id']
    if reason == 'wrong-character':
        ref = source(store, 'momo')
    elif reason == 'missing':
        ref = FactRef(kind='EVENT', id=2147483647)
    elif reason == 'simulated':
        ref = source(store, experience_scope=SIM)
    else:
        ref = source(store, experience_scope={'schema_version': True, 'reality': 'REAL'})
    with pytest.raises(EvidenceUnavailable) as error:
        repo.act('rin', gid, complete(1, [ref]))
    assert 'PRIVATE_SOURCE_BODY' not in str(error.value)
    assert repo.get('rin', gid)['version'] == 1
    assert repo.get('rin', gid)['completions'] == []


@pytest.mark.parametrize('witness', ['own', 'trace', 'membership-only'])
def test_group_evidence_requires_authored_or_actually_witnessed_fact(state, witness):
    store, repo = state
    groups = GroupRepository(store)
    group = groups.create_group('shared', ['rin', 'momo'], NOW)
    event = groups.append_event(GroupEvent(conversation_id=group.id, turn_id='turn', actor_type='CHARACTER',
        actor_id='rin' if witness == 'own' else 'momo', event_type='CHARACTER_MESSAGE', event_time=NOW, content='PRIVATE_GROUP_BODY'))
    if witness == 'trace':
        groups.add_trace(group.id, 'turn', 'rin', event.id, NOW, {'perceived': True})
    ref = FactRef(kind='CONVERSATION_EVENT', id=event.id, conversation_id=group.id)
    gid = repo.create('rin', create_request())['goal']['id']
    if witness == 'membership-only':
        with pytest.raises(EvidenceUnavailable):
            repo.act('rin', gid, complete(1, [ref]))
    else:
        accepted = repo.act('rin', gid, complete(1, [ref]))['goal']
        assert accepted['completions'][0]['evidence'][0]['status'] == 'CURRENT'
        if witness == 'trace':
            store.conn.execute("DELETE FROM conversation_runtime_traces WHERE character_id='rin'")
            store.conn.commit()
            current = repo.get('rin', gid)
            assert current['completions'][0]['evidence'][0]['status'] == 'UNAVAILABLE'
            assert 'PRIVATE_GROUP_BODY' not in json.dumps(current)


@pytest.mark.parametrize('mutation,expected', [('content', 'CHANGED'), ('scope', 'UNAVAILABLE'), ('delete', 'UNAVAILABLE')])
def test_live_projection_reports_source_change_without_returning_body(state, mutation, expected):
    store, repo = state
    ref = source(store)
    gid = repo.create('rin', create_request(stages=['one']))['goal']['id']
    accepted = repo.act('rin', gid, complete(1, [ref]))['goal']
    entry = accepted['completions'][0]['evidence'][0]
    assert entry['status'] == 'CURRENT' and entry['accepted_digest']
    if mutation == 'content':
        store.conn.execute("UPDATE conversation_events SET content='PRIVATE_CHANGED_BODY' WHERE id=?", (ref.id,))
    elif mutation == 'scope':
        store.conn.execute('UPDATE conversation_events SET metadata_json=? WHERE id=?', (json.dumps({'experience_scope': SIM}), ref.id))
    else:
        store.conn.execute('DELETE FROM conversation_events WHERE id=?', (ref.id,))
    store.conn.commit()
    goal = repo.get('rin', gid)
    assert goal['phase'] == 'COMPLETED'
    assert goal['completions'][0]['evidence'][0]['status'] == expected
    assert 'PRIVATE_SOURCE_BODY' not in json.dumps(goal) and 'PRIVATE_CHANGED_BODY' not in json.dumps(goal)
    assert goal['completions'][0]['evidence'][0]['accepted_digest'] == entry['accepted_digest']


def test_completion_write_failure_rolls_back_goal_stage_and_request(state):
    store, repo = state
    gid = repo.create('rin', create_request())['goal']['id']
    ref = source(store)
    request = complete(1, [ref])
    def forbid_insert(code, *args):
        return sqlite3.SQLITE_DENY if code == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK
    store.conn.set_authorizer(forbid_insert)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            repo.act('rin', gid, request)
    finally:
        store.conn.set_authorizer(None)
    goal = repo.get('rin', gid)
    assert goal['version'] == 1 and goal['current_stage'] == 0 and goal['completions'] == []
    result = repo.act('rin', gid, request)
    assert not result['replayed'] and result['goal']['version'] == 2


def test_uninitialized_read_does_not_create_tables_or_migrations(tmp_path):
    store = SQLiteStore(tmp_path / 'readonly.sqlite')
    def forbid_write(code, *args):
        return sqlite3.SQLITE_DENY if code in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_ALTER_TABLE} else sqlite3.SQLITE_OK
    before = store.conn.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    store.conn.set_authorizer(forbid_write)
    try:
        repo = GoalRepository(store, initialize=False)
        assert repo.list('rin') == []
        with pytest.raises(GoalMissing):
            repo.get('rin', 'missing')
        assert store.conn.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall() == before
    finally:
        store.conn.set_authorizer(None)
        store.close()


@pytest.mark.parametrize('changes', [{'title': ' '}, {'stages': []}, {'stages': [' ']}, {'stages': ['a'] * 9},
                                     {'success_criteria': ' '}, {'title': 'a' * 161}, {'owner_id': 'other'}])
def test_create_schema_rejects_unsupported_or_empty_fields(changes):
    with pytest.raises(ValidationError):
        create_request(**changes)


@pytest.mark.parametrize('changes', [{'expected_version': True}, {'confirmation': None}, {'evidence': []},
                                     {'confirmation': 'MODEL_ACCEPTED'}, {'extra_owner': 'other'}])
def test_stage_action_requires_strict_version_explicit_confirmation_and_evidence(changes):
    data = dict(request_id=str(uuid4()), expected_version=1, operation='COMPLETE_STAGE',
                confirmation='OPERATOR_ACCEPTED', evidence=[FactRef(kind='EVENT', id=1)])
    data.update(changes)
    with pytest.raises(ValidationError):
        GoalAction(**data)


def test_completed_stage_persists_without_copied_source_or_new_world_fact(tmp_path):
    path = tmp_path / 'restart-goals.sqlite'
    store = SQLiteStore(path)
    before_tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    repo = GoalRepository(store)
    goal_tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} - before_tables
    ref = source(store)
    request = create_request(stages=['one'])
    created = repo.create('rin', request)
    gid = created['goal']['id']
    acceptance = complete(1, [ref])
    acknowledged = repo.act('rin', gid, acceptance)
    assert len(store.list_events('rin')) == 1 and store.list_memories('rin') == []
    for table in goal_tables:
        values = [tuple(row) for row in store.conn.execute('SELECT * FROM "' + table + '"')]
        assert 'PRIVATE_SOURCE_BODY' not in str(values)
    store.close()
    reopened = SQLiteStore(path)
    try:
        repo = GoalRepository(reopened)
        replay = repo.act('rin', gid, acceptance)
        assert replay['replayed'] and replay['receipt'] == acknowledged['receipt']
        assert replay['goal']['phase'] == 'COMPLETED' and replay['goal']['version'] == 2
        assert len(replay['goal']['completions']) == 1
        assert len(reopened.list_events('rin')) == 1
    finally:
        reopened.close()


def test_duplicate_evidence_is_refused_before_stage_advancement(state):
    store, repo = state
    gid = repo.create('rin', create_request())['goal']['id']
    ref = source(store)
    with pytest.raises((ValidationError, GoalConflict)):
        repo.act('rin', gid, complete(1, [ref, ref]))
    assert repo.get('rin', gid)['version'] == 1
