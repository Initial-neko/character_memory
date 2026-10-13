import os
import socket
import threading
import time
from datetime import datetime, timezone
import pytest
from test_life_conversation import isolated_app
from character_memory.domain.models import Event, EventType

pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='explicit browser run')]


@pytest.fixture
def live_life(isolated_app):
    import uvicorn
    app=isolated_app;access=app.state.character_memory;s=access.store()
    profiles=access.character_profiles();character=profiles[0]['id']
    e=s.append_event(Event(character_id=character,event_type=EventType.USER_MESSAGE,event_time=datetime.now(timezone.utc),content='浏览器原始事实 <script>window.FORGED=true</script>',metadata={'conversation_id':'browser-existing'}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',lifespan='off'))
    thread=threading.Thread(target=server.run);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        assert server.started
        yield f'http://127.0.0.1:{port}',character,e,s,access
    finally:
        access.stream_hub.close()
        server.should_exit=True;thread.join(10)
        assert not thread.is_alive()


def test_real_chromium_draft_cancel_and_confirm_only(page,live_life):
    from playwright.sync_api import expect
    base,character,event,store,access=live_life
    requests=[];page.on('request',lambda r:requests.append((r.method,r.url)))
    page.goto(f'{base}/life?character={character}')
    page.get_by_role('button',name='围绕这件事聊天').click()
    expect(page.locator('#messageInput')).to_have_value('想和你聊聊这条记录。')
    expect(page.get_by_role('button',name='取消引用')).to_be_visible()
    assert store.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1
    assert not [r for r in requests if r[0]=='POST' and r[1].endswith('/messages')]
    assert page.evaluate('window.FORGED') is None
    page.get_by_role('button',name='取消引用').click()
    expect(page.locator('#messageInput')).to_have_value('')
    page.goto(f'{base}/?life_character={character}&life_key=events:{event.id}')
    expect(page.get_by_role('button',name='取消引用')).to_be_visible()
    page.locator('#messageInput').fill('想听听你的看法')
    page.locator('#sendButton').click()
    expect(page.get_by_role('button',name='取消引用')).to_have_count(0)
    rows=store.list_events(character)
    assert len(rows)==2
    assert rows[-1].metadata['life_reference']=={'character_id':character,'key':f'events:{event.id}'}
    assert '浏览器原始事实' in rows[-1].content
    assert page.evaluate('window.CM.state.lifeReference') is None
    assert page.request.get(base+'/health').json()['runtime_loaded'] is False


def test_real_chromium_deleted_source_keeps_draft(page,live_life):
    from playwright.sync_api import expect
    base,character,event,store,access=live_life
    page.goto(f'{base}/?life_character={character}&life_key=events:{event.id}')
    expect(page.get_by_role('button',name='取消引用')).to_be_visible()
    store.conn.execute('DELETE FROM events WHERE id=?',[event.id]);store.conn.commit()
    page.locator('#sendButton').click()
    expect(page.locator('p[aria-live="polite"]')).to_contain_text('草稿已保留')
    expect(page.locator('#messageInput')).to_have_value('想和你聊聊这条记录。')
    assert store.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    assert page.request.get(base+'/health').json()['runtime_loaded'] is False


def test_real_chromium_group_source_enters_original_room(page,live_life):
    from playwright.sync_api import expect
    from character_memory.group_store import GroupRepository, GroupEvent
    base,character,event,store,access=live_life
    other=next(p['id'] for p in access.character_profiles() if p['id']!=character)
    repo=GroupRepository(store);now=datetime.now(timezone.utc)
    group=repo.create_group('原始来源群',[character,other],now)
    source=repo.append_event(GroupEvent(conversation_id=group.id,turn_id='original',actor_type='CHARACTER',actor_id=character,event_type='CHARACTER_MESSAGE',event_time=now,content='群中实际交流'))
    page.goto(f'{base}/?life_character={character}&life_key=group:{source.id}')
    expect(page.get_by_role('button',name='取消引用')).to_be_visible()
    assert page.evaluate('window.CM.state.conversation.groupId')==group.id
    assert len(repo.list_events(group.id))==1
    page.locator('#sendButton').click()
    expect(page.get_by_role('button',name='取消引用')).to_have_count(0)
    assert len(repo.list_events(group.id))==2
    assert repo.list_events(group.id)[-1].metadata['life_reference']['key']==f'group:{source.id}'
    assert page.request.get(base+'/health').json()['runtime_loaded'] is False
