from datetime import timedelta
from test_observed_event_recall import store, observe, NOW
from character_memory.space_store import SpaceRepository


def publish(store, *, channel='WORLD', owner='a'):
    event=observe(store,channel=channel,content='私人秘密感受')
    repo=SpaceRepository(store)
    post=repo.create_post(owner,'公开谈论配色',NOW+timedelta(minutes=1))
    store.update_event_metadata(event.id,{**event.metadata,'public_projection':{'post_id':post.id,'content':post.content}})
    return event,post


def test_public_world_projection_only_matches_published_content(store):
    event,post=publish(store)
    assert not store.recall_observed_events('a','配色',at=NOW,projection='PUBLIC')
    found=store.recall_observed_events('a','配色',at=NOW+timedelta(minutes=2),projection='PUBLIC')
    assert found[0].id==event.id and found[0].content==post.content
    assert 'world_summary' not in found[0].metadata
    assert not store.recall_observed_events('a','秘密',at=NOW+timedelta(minutes=2),projection='PUBLIC')
    assert store.recall_observed_events('a','秘密',at=NOW+timedelta(minutes=2))
    store.conn.execute('DELETE FROM space_posts');store.conn.commit()
    assert not store.recall_observed_events('a','配色',at=NOW+timedelta(minutes=2),projection='PUBLIC')


def test_public_world_requires_matching_owner_and_scope(store):
    publish(store,owner='b')
    publish(store,channel='PERSONAL_RSS')
    observe(store,channel='WORLD',content='未发布配色')
    assert not store.recall_observed_events('a','配色',at=NOW+timedelta(minutes=2),projection='PUBLIC')
