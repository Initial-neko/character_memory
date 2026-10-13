from datetime import datetime, timedelta, timezone

import pytest

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.domain.models import Event, EventType
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore
from test_character_architecture_baseline import CountingEmbedding, CountingModel

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize('entry', ['wake', 'intent'])
def test_proactive_sharing_recovers_sourced_observation_without_extra_reaction(tmp_path, entry):
    store = SQLiteStore(tmp_path / 'sharing.db')
    try:
        observed = store.append_event(Event(character_id='a', event_type=EventType.WORLD_OBSERVATION,
            event_time=NOW-timedelta(hours=1), content='我读了公开的紫杉醇研究报道。',
            metadata={'channel': 'PERSONAL_BROWSE', 'sources': ['https://example.com/research']}))
        for index in range(10):
            store.append_event(Event(character_id='a', event_type=EventType.USER_MESSAGE,
                event_time=NOW-timedelta(minutes=20-index), content='普通聊天', metadata={'conversation_id': 'direct'}))
        model, embedding = CountingModel(), CountingEmbedding()
        runtime = PersonRuntime(store, VectorRecall(store, embedding), embedding, model, 'persona')
        chat = ChatService(store, runtime, FixedClock(NOW))
        result = (chat.dispatch_wake(character_id='a', at=NOW) if entry == 'wake' else
                  chat.dispatch_proactive_intent(character_id='a', intent_id=1, content='想联系用户', at=NOW))
        candidates = result.event.metadata['sharing_observations']
        assert [item['event_id'] for item in candidates] == [observed.id]
        assert candidates[0]['sources'] == ['https://example.com/research']
        assert 'https://example.com/research' in result.context
        assert '只想联系用户' in result.context
        assert '不得推测用户此刻的心情' in result.context
        assert [call[0] for call in model.calls] == ['PersonReaction']
        assert result.reaction.actions == []
        assert store.list_memories('a') == []
    finally:
        store.close()


def test_sharing_context_is_bounded_and_excludes_unsourced_private_and_future_facts(tmp_path):
    store = SQLiteStore(tmp_path / 'sharing.db')
    try:
        for index in range(6):
            store.append_event(Event(character_id='a', event_type=EventType.WORLD_OBSERVATION,
                event_time=NOW-timedelta(minutes=30-index), content='有来源的观察' * 1000,
                metadata={'channel': 'PERSONAL_BROWSE', 'sources': ['https://example.com/' + 'x'*500]}))
        for character, kind, channel, sources, at in [
            ('a', EventType.WORLD_OBSERVATION, 'PERSONAL_BROWSE', [], NOW),
            ('a', EventType.WORLD_OBSERVATION, 'WORLD', ['private'], NOW),
            ('a', EventType.SPACE_COMMENT_RECEIVED, 'SPACE', ['private'], NOW),
            ('b', EventType.WORLD_OBSERVATION, 'PERSONAL_BROWSE', ['other'], NOW),
            ('a', EventType.WORLD_OBSERVATION, 'PERSONAL_BROWSE', ['future'], NOW+timedelta(days=1)),
        ]:
            store.append_event(Event(character_id=character, event_type=kind, event_time=at,
                content='excluded', metadata={'channel': channel, 'sources': sources}))
        result = ChatService(store, type('Recorder', (), {'handle': lambda self, event: event})(), FixedClock(NOW)).dispatch_wake(character_id='a', at=NOW)
        candidates = result.metadata['sharing_observations']
        assert len(candidates) == 4
        assert all(len(item['content']) <= 600 for item in candidates)
        assert all(len(item['sources'][0]) <= 240 for item in candidates)
        assert all(item['channel'] == 'PERSONAL_BROWSE' for item in candidates)
        assert all('excluded' not in item['content'] for item in candidates)
    finally:
        store.close()

@pytest.mark.parametrize('entry', ['wake', 'intent'])
def test_proactive_sharing_reuses_existing_image_delivery_and_source_receipt(tmp_path, entry):
    from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
    from character_memory.images import CharacterImage, ImageCatalog
    from character_memory.llm.client import ModelCallResult, ModelCallTrace

    class ShareModel(CountingModel):
        def react_call_for_session(self, context, session_id):
            self.calls.append(('PersonReaction', session_id, context))
            return ModelCallResult(value=PersonReaction(actions=[
                ActionDecision(type=ActionType.MESSAGE, message='想给你看看这张图。'),
                ActionDecision(type=ActionType.IMAGE, image_id='old_photo'),
                ActionDecision(type=ActionType.GENERATE_IMAGE, image_purpose='SCENE', visual_intent='new image'),
            ]), trace=ModelCallTrace(attempt=1, model='local'))

    (tmp_path / 'photo.png').write_bytes(b'local asset')
    images = ImageCatalog(tmp_path, [CharacterImage(id='old_photo', file='photo.png', label='已有照片')], source='character')
    store = SQLiteStore(tmp_path / 'image.db')
    try:
        model, embedding = ShareModel(), CountingEmbedding()
        runtime = PersonRuntime(store, VectorRecall(store, embedding), embedding, model, 'persona', image_catalog=images)
        chat = ChatService(store, runtime, FixedClock(NOW))
        result = (chat.dispatch_wake(character_id='a', at=NOW) if entry == 'wake' else
                  chat.dispatch_proactive_intent(character_id='a', intent_id=1, content='想联系用户', at=NOW))
        messages = store.list_chat_events('a')
        assert [item.metadata['action'] for item in messages] == ['MESSAGE', 'IMAGE']
        assert all(item.metadata['source_event_id'] == result.event.id for item in messages)
        assert messages[1].metadata['image_id'] == 'old_photo'
        assert all(action.type != ActionType.GENERATE_IMAGE for action in result.reaction.actions)
        assert len(model.calls) == 1
    finally:
        store.close()


def test_sharing_preserves_reading_scope_and_does_not_duplicate_lexical_recall(tmp_path):
    store = SQLiteStore(tmp_path / 'scope.db')
    try:
        reading = {'title': '紫杉醇报道', 'content_scope': 'RSS_FEED_TEXT', 'truncated': True,
                   'read_characters': 300, 'available_characters': 600}
        observed = store.append_event(Event(character_id='a', event_type=EventType.WORLD_OBSERVATION,
            event_time=NOW-timedelta(minutes=2), content='读了紫杉醇报道的Feed节选。',
            metadata={'channel': 'PERSONAL_BROWSE', 'sources': ['https://example.com/research'], 'rss_reading': reading}))
        model, embedding = CountingModel(), CountingEmbedding()
        runtime = PersonRuntime(store, VectorRecall(store, embedding), embedding, model, 'persona')
        result = ChatService(store, runtime, FixedClock(NOW)).dispatch_proactive_intent(
            character_id='a', intent_id=1, content='分享紫杉醇报道', at=NOW)
        assert result.context.count(f'event_id={observed.id} ') == 1
        assert 'scope="RSS_FEED_TEXT" truncated=true read_characters=300 available_characters=600 linked_original_not_fetched=true' in result.context
        assert store.get_event(observed.id).metadata['rss_reading'] == reading
        block = result.context.split('# Personal Sharing Observations\n')[1].split('\n#')[0]
        assert len(block) < 3400
    finally:
        store.close()


def test_sharing_candidate_window_does_not_scan_unbounded_history(tmp_path):
    store = SQLiteStore(tmp_path / 'window.db')
    try:
        store.append_event(Event(character_id='a', event_type=EventType.WORLD_OBSERVATION,
            event_time=NOW-timedelta(hours=1), content='旧观察', metadata={'channel': 'PERSONAL_BROWSE', 'sources': ['https://example.com/old']}))
        for index in range(32):
            store.append_event(Event(character_id='a', event_type=EventType.USER_MESSAGE,
                event_time=NOW-timedelta(minutes=32-index), content='新聊天'))
        result = ChatService(store, type('Recorder', (), {'handle': lambda self, event: event})(), FixedClock(NOW)).dispatch_wake(character_id='a', at=NOW)
        assert result.metadata['sharing_observations'] == []
    finally:
        store.close()
