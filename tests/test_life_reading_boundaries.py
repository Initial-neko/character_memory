from character_memory.character_life import CharacterLifeReader
from character_memory.runtime.context import render_observed_experiences
from character_memory.domain.models import Event,EventType
from test_character_life import store,NOW,event


def test_feed_title_scope_and_truncation_are_facts_separate_from_appraisal(store):
    e=store.append_event(Event(character_id='a',event_type=EventType.WORLD_OBSERVATION,event_time=NOW,content='my note',metadata={'world_summary':'model interpretation','rss_reading':{'title':'article','content_scope':'RSS_FEED_TEXT','read_characters':80,'available_characters':200,'truncated':True}}))
    item=CharacterLifeReader(store).timeline('a')['items'][0]
    assert item['title']=='阅读了 RSS Feed 文本：article'
    assert item['reading']['truncated'] is True and item['reading']['read_characters']==80
    rendered=render_observed_experiences([e])
    assert 'truncated=true' in rendered and 'available_characters=200' in rendered
    assert '人物评估摘要' in rendered


def test_invalid_intent_time_is_unknown_not_pending(store):
    store.add_intent('a','future','none',NOW,NOW,NOW)
    store.conn.execute("UPDATE intents SET earliest_at='broken'");store.conn.commit()
    item=CharacterLifeReader(store).timeline('a',view='intents')['items'][0]
    assert item['intent']['time_status']=='INVALID'


def test_undated_records_reported_without_character_leak(store):
    e=event(store);other=event(store,'b')
    store.conn.execute("UPDATE events SET event_time='broken' WHERE id IN (?,?)",[e.id,other.id]);store.conn.commit()
    page=CharacterLifeReader(store).timeline('a',day='2026-10-09')
    assert page['items']==[] and page['undated_count']==1


def test_empty_required_intent_time_is_invalid(store):
    store.add_intent('a','future','none',NOW,NOW,NOW)
    store.conn.execute("UPDATE intents SET earliest_at=''");store.conn.commit()
    item=CharacterLifeReader(store).timeline('a',view='intents')['items'][0]
    assert item['intent']['time_status']=='INVALID' and not item['intent']['due']
