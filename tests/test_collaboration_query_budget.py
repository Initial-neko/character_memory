"""Relationship reads must not multiply shared facts by unrelated ACTION rows."""
from datetime import datetime, timezone
import sqlite3

import pytest

from character_memory.character_life import CharacterLifeReader
from character_memory.domain.models import Event, EventType
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.storage.sqlite import SQLiteStore


def test_relationship_query_stays_within_instruction_budget(tmp_path):
    store = SQLiteStore(tmp_path / 'relationships.db')
    now = datetime.now(timezone.utc)
    groups = GroupRepository(store)
    group = groups.create_group('shared', ['alice', 'bob'], now)
    for number in range(400):
        groups.append_event(GroupEvent(conversation_id=group.id, turn_id=str(number),
            actor_type='CHARACTER', actor_id='bob', event_type='CHARACTER_MESSAGE',
            event_time=now, content='ordinary shared fact'))
    for number in range(1000):
        metadata = {'action': 'MESSAGE'} if number < 500 else {
            'fact_kind': 'ROLE_COLLABORATION_MESSAGE', 'channel': 'GROUP',
            'channel_id': group.id, 'shared_fact_id': 10000 + number,
            'actor_id': 'bob', 'recipient_id': 'alice', 'direction': 'RECEIVED',
        }
        store.append_event(Event(character_id='alice', event_type=EventType.ACTION,
            event_time=now, content='unrelated action', metadata=metadata))
    source = store.append_event(Event(character_id='alice', event_type=EventType.USER_MESSAGE,
        event_time=now, content='actual direct interaction'))
    before = store.conn.total_changes
    instructions = 0
    def budget():
        nonlocal instructions
        instructions += 1000
        return instructions > 1_000_000
    store.conn.set_progress_handler(budget, 1000)
    try:
        try:
            page = CharacterLifeReader(store).timeline('alice', view='relationships', peer='user', limit=4)
        except sqlite3.OperationalError as error:
            pytest.fail(f'relationship query exceeded instruction budget: {error}')
        assert [item['key'] for item in page['items']] == [f'events:{source.id}']
        assert store.conn.total_changes == before
    finally:
        store.conn.set_progress_handler(None, 0)
        store.close()


def test_existing_database_gets_collaboration_index_without_rewriting_events(tmp_path):
    path = tmp_path / 'legacy.db'
    store = SQLiteStore(path)
    store.conn.execute('DROP INDEX IF EXISTS idx_events_collaboration_fact')
    store.conn.execute("DELETE FROM schema_migrations WHERE name='core/011-collaboration-fact-index'")
    store.conn.execute("INSERT INTO events(character_id,event_type,event_time,content,metadata_json) VALUES('alice','ACTION','invalid','legacy','malformed-json')")
    store.conn.commit()
    original = [tuple(row) for row in store.conn.execute('SELECT * FROM events')]
    store.close()
    migrated = SQLiteStore(path)
    try:
        assert migrated.conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_events_collaboration_fact'").fetchone()
        assert [tuple(row) for row in migrated.conn.execute('SELECT * FROM events')] == original
        applied = migrated.conn.execute("SELECT applied_at FROM schema_migrations WHERE name='core/011-collaboration-fact-index'").fetchone()[0]
    finally:
        migrated.close()
    reopened = SQLiteStore(path)
    try:
        assert reopened.conn.execute("SELECT applied_at FROM schema_migrations WHERE name='core/011-collaboration-fact-index'").fetchone()[0] == applied
    finally:
        reopened.close()


def test_receipt_integer_cast_cannot_alias_an_overflowing_fact_id(tmp_path):
    store = SQLiteStore(tmp_path / 'overflow.db')
    now = datetime.now(timezone.utc)
    groups = GroupRepository(store)
    group = groups.create_group('shared', ['alice', 'bob'], now)
    fact = groups.append_event(GroupEvent(conversation_id=group.id, turn_id='overflow',
        actor_type='CHARACTER', actor_id='bob', event_type='CHARACTER_MESSAGE',
        event_time=now, content='not received by alice'))
    store.conn.execute('UPDATE conversation_events SET id=? WHERE id=?', (2**63 - 1, fact.id))
    store.conn.commit()
    store.append_event(Event(character_id='alice', event_type=EventType.ACTION,
        event_time=now, content='invalid receipt', metadata={
            'fact_kind': 'ROLE_COLLABORATION_MESSAGE', 'channel': 'GROUP',
            'channel_id': group.id, 'shared_fact_id': 2**63,
            'actor_id': 'bob', 'recipient_id': 'alice', 'direction': 'RECEIVED',
        }))
    try:
        assert CharacterLifeReader(store).timeline('alice', view='relationships', peer='bob')['items'] == []
    finally:
        store.close()
