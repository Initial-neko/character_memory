from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from character_memory.domain.models import Event, EventType, Memory
from character_memory.runtime.context import compile_context


OLD = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 25, 9, tzinfo=timezone.utc)


def memory(kind='USER'):
    return Memory(character_id='rin', content='明天一起整理书桌', memory_type=kind,
                  event_time=OLD, source_event_id=17)


@pytest.mark.parametrize('channel', ['DIRECT', 'GROUP'])
@pytest.mark.parametrize('kind', ['USER', 'EPISODIC'])
def test_old_memory_keeps_its_own_time_and_source(channel, kind):
    event = Event(character_id='rin', event_type=EventType.USER_MESSAGE, event_time=NOW,
                  content='上次说的整理书桌是什么时候？', metadata={'channel': channel})
    prompt = compile_context('persona', '', [memory(kind)], event)
    assert 'memory_event_time=2026-09-01T09:00:00+00:00' in prompt
    assert 'source_event_id=17' in prompt
    assert '明天一起整理书桌' in prompt


def test_space_daily_prompt_preserves_memory_time():
    from character_memory.space_autonomy import SpaceAutonomyService
    snapshot = SimpleNamespace(persona='persona', mental_state='', memories=[memory()], recent_events=[], observed_events=[])
    runtime = SimpleNamespace(context_builder=SimpleNamespace(build=lambda *a, **kw: snapshot))
    service = SpaceAutonomyService.__new__(SpaceAutonomyService)
    service.access = SimpleNamespace(settings=SimpleNamespace(space_media_enabled=False))
    prompt = service._daily_context('rin', NOW, runtime)
    assert 'memory_event_time=2026-09-01T09:00:00+00:00' in prompt
    assert 'source_event_id=17' in prompt


def test_world_memory_projection_preserves_time_and_existing_limit():
    from character_memory.world_activity import WorldActivityService
    snapshot = SimpleNamespace(memories=[memory(), Memory(character_id='rin', content='outside limit', event_time=NOW)])
    prompt = WorldActivityService._memory_text(snapshot, limit=1)
    assert 'memory_event_time=2026-09-01T09:00:00+00:00' in prompt
    assert 'source_event_id=17' in prompt
    assert 'outside limit' not in prompt


def test_legacy_missing_metadata_is_unknown_not_current_time():
    from character_memory.runtime.prompt_parts import render_memory_lines
    prompt = render_memory_lines([SimpleNamespace(memory_type='USER', content='a legacy fact')])
    assert 'memory_event_time=unknown' in prompt
    assert 'source_event_id=unknown' in prompt
    assert 'a legacy fact' in prompt
    assert render_memory_lines([]) == '- 无'


def test_equivalent_timezones_and_legacy_naive_have_same_anchor():
    from character_memory.runtime.prompt_parts import render_memory_lines
    offsets = [OLD, OLD.astimezone(timezone(timedelta(hours=8))), OLD.replace(tzinfo=None)]
    outputs = [render_memory_lines([memory().model_copy(update={'event_time': value})]) for value in offsets]
    assert len(set(outputs)) == 1


def test_restart_retains_source_anchor_without_writes(tmp_path):
    from character_memory.storage.sqlite import SQLiteStore
    from character_memory.runtime.prompt_parts import render_memory_lines
    store = SQLiteStore(tmp_path / 'memory.db')
    event = store.append_event(Event(character_id='rin', event_type=EventType.USER_MESSAGE,
                                    event_time=OLD, content='明天一起整理书桌'))
    store.add_memory(memory().model_copy(update={'source_event_id': event.id}))
    store.close()
    reopened = SQLiteStore(tmp_path / 'memory.db')
    try:
        before = reopened.conn.total_changes
        output = render_memory_lines(reopened.list_memories('rin', include_embedding=False))
        assert 'memory_event_time=2026-09-01T09:00:00+00:00' in output
        assert f'source_event_id={event.id}' in output
        assert reopened.conn.total_changes == before
    finally:
        reopened.close()
