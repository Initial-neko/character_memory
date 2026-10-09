from test_character_life import store, NOW, event
from character_memory.character_life import CharacterLifeReader
from character_memory.domain.models import Memory
from character_memory.group_store import GroupRepository,GroupEvent
from character_memory.space_store import SpaceRepository


def test_group_origin_has_precedence_over_legacy_core_numeric_source(store):
    e=event(store)
    store.add_memory(Memory(character_id='a',content='Group memory',event_time=NOW,source_event_id=e.id,embedding=[1],metadata={'origin':'GROUP','conversation_id':'different','source_conversation_event_id':1}))
    item=CharacterLifeReader(store).timeline('a')['items'][0]
    assert item['memories']==[]
    assert CharacterLifeReader(store).timeline('a',view='changes')['items']==[]


def test_own_group_expression_does_not_claim_self_as_relationship(store):
    repo=GroupRepository(store);g=repo.create_group('g',['a','b'],NOW)
    repo.append_event(GroupEvent(conversation_id=g.id,turn_id='t',actor_type='CHARACTER',actor_id='a',event_type='CHARACTER_MESSAGE',event_time=NOW,content='own'))
    assert CharacterLifeReader(store).timeline('a',channel='GROUP')['items'][0]['participants']==[]


def test_explicit_reply_counterpart_overrides_post_author(store):
    repo=SpaceRepository(store)
    post=repo.create_post('a','own',NOW)
    parent=repo.add_comment(post.id,'b','comment',NOW)
    reply=repo.add_comment(post.id,'a','reply',NOW,reply_to_comment_id=parent.id)
    records=CharacterLifeReader(store).timeline('a',view='relationships',peer='b')['items']
    assert len(records)==1
    assert records[0]['source']['id']==reply.id
    assert records[0]['participants']==[{'id':'b','name':'b'}]


def test_malformed_group_memory_metadata_does_not_break_real_experience(store):
    repo=GroupRepository(store);g=repo.create_group('g',['a','b'],NOW)
    e=repo.append_event(GroupEvent(conversation_id=g.id,turn_id='t',actor_type='CHARACTER',actor_id='b',event_type='CHARACTER_MESSAGE',event_time=NOW,content='source'))
    repo.add_trace(g.id,'t','a',e.id,NOW,{'actions':[]})
    m=store.add_memory(Memory(character_id='a',content='malformed provenance',event_time=NOW,embedding=[1]))
    store.conn.execute("UPDATE memories SET metadata_json=? WHERE id=?",['{broken',m.id])
    page=CharacterLifeReader(store).timeline('a',channel='GROUP')
    assert len(page['items'])==1 and page['items'][0]['memories']==[]
