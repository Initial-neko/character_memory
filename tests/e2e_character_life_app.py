"""Isolated real life projection/routes with explicitly seeded acceptance records."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from character_memory.storage.sqlite import SQLiteStore
from character_memory.domain.models import Event,EventType,Memory
from character_memory.character_life_web import attach_character_life_routes
from character_memory.world_activity import WorldPulseRepository
from character_memory.space_store import SpaceRepository


def build(db_path):
    store=SQLiteStore(db_path)
    now=datetime.now(timezone.utc).astimezone().replace(hour=9,minute=20,second=0,microsecond=0)
    e=store.append_event(Event(character_id='a',event_type=EventType.WORLD_OBSERVATION,event_time=now,content='对绘画配色产生了兴趣',metadata={'rss_reading':{'title':'配色笔记','content_scope':'RSS_FEED_TEXT','url':'https://example.com/article'},'world_summary':'实际阅读的 RSS 节选摘要'}))
    changed=store.append_event(Event(character_id='a',event_type=EventType.SPACE_COMMENT_RECEIVED,event_time=now.replace(hour=14,minute=30),content='围绕冷暖配色讨论，各自保留不同看法。'))
    store.set_mental_state('a','对冷暖配色感兴趣，想了解不同处理方法。',now)
    store.set_mental_state('a','开始留意冷暖对比，也保留自己偏好的柔和表达。',changed.event_time,changed.id)
    store.add_memory(Memory(character_id='a',content='讨论过冷暖配色',event_time=changed.event_time,source_event_id=changed.id,embedding=[1.0]))
    store.add_runtime_trace('a',changed.id,changed.event_time,{'actions':[{'type':'SPACE_COMMENT'}],'reaction':'回应了配色讨论，不改变自己的偏好。'})
    quiet=store.append_event(Event(character_id='a',event_type=EventType.PROACTIVE_INTENT,event_time=now.replace(hour=18),content='一次真实主动表达机会'))
    store.add_runtime_trace('a',quiet.id,quiet.event_time,{'actions':[]})
    WorldPulseRepository(store)
    store.conn.execute("INSERT INTO world_browse_decisions(opportunity_id,character_id,started_at,phase,plan_json,result_json) VALUES(?,?,?,?,?,?)",['world:a','a',now.replace(hour=18,minute=15).isoformat(),'APPLIED','{"decision":{"choice":"NO_ACTION"}}','{"execution_status":"SKIPPED"}'])
    store.add_intent('a','明天继续讨论配色','PROACTIVE_MESSAGE',now.replace(hour=19),now+timedelta(days=1),now+timedelta(days=3))
    for n in range(35):
        store.append_event(Event(character_id='a',event_type=EventType.USER_MESSAGE,event_time=now-timedelta(minutes=n+1),content=f'真实分页测试 {n}'))
    store.append_event(Event(character_id='b',event_type=EventType.USER_MESSAGE,event_time=now,content='另一个人物独立经历'))
    app=FastAPI();web=Path(__file__).parents[1]/'src/character_memory/web'
    def ensure(cid):
        if cid not in {'a','b'}:raise HTTPException(404,'unknown')
    attach_character_life_routes(app,SimpleNamespace(read_store=store,ensure_character=ensure,web_dir=web))
    app.mount('/static',StaticFiles(directory=web),name='static')
    @app.get('/v1/characters')
    def profiles():return {'characters':[{'id':'a','name':'林绘 · 隔离验收'},{'id':'b','name':'另一位角色'}]}
    return app,store
