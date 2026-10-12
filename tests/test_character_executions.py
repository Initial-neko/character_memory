from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from character_memory.storage.sqlite import SQLiteStore
from character_memory.runtime.capability_execution import CapabilityExecutor
from character_memory.character_life_web import attach_character_life_routes
from test_claude_world import Runner, limits
from test_claude_process import request, Observer


def test_execution_projection_is_readonly_and_private(tmp_path):
    from character_memory.character_life import CharacterExecutionReader
    store=SQLiteStore(tmp_path/"local.db")
    req=replace(request(),constraints={**request().constraints,**limits()})
    CapabilityExecutor(store,Observer(),claude_runner=Runner()).execute(req)
    reader=CharacterExecutionReader(store,tmp_path/"claude-executions")
    before=store.conn.total_changes
    page=reader.list("alice")
    assert page["read_only"] and len(page["items"])==1
    assert page["summary"]["known_estimated_cost_usd"]==.02
    assert page["summary"]["unknown_usage_records"]==0
    day=page["items"][0]["execution"]["day"]
    assert reader.list("alice",day=day)["summary"]["executions"]==1
    assert reader.list("alice",day="2000-01-01")["summary"]["executions"]==0
    # Local quota day may differ from the timestamp's written UTC date.
    metadata=page["items"][0]["execution"]
    metadata["day"]="2000-01-01"
    store.conn.execute("UPDATE capability_executions SET execution_metadata_json=?",[json.dumps(metadata)])
    store.conn.commit()
    assert len(reader.list("alice",day="2000-01-01")["items"])==1
    assert reader.list("alice",day="2000-01-01")["summary"]["executions"]==1
    before=store.conn.total_changes
    detail=reader.detail("alice",req.request_id)
    assert detail["task"]=="public photography" and detail["details_state"]=="NOT_RECORDED"
    assert reader.list("bob")["items"]==[]
    assert reader.detail("bob",req.request_id) is None
    assert store.conn.total_changes==before
    store.close()


def test_pagination_and_incomplete_usage_remain_explicit(tmp_path):
    from character_memory.character_life import CharacterExecutionReader
    store=SQLiteStore(tmp_path/"local.db")
    runner=Runner()
    base=replace(request(),constraints={**request().constraints,**limits()})
    executor=CapabilityExecutor(store,Observer(),claude_runner=runner)
    for i in range(3):
        executor.execute(replace(base,request_id=f"run:{i}",opportunity_id=f"run:{i}"))
    store.conn.execute("UPDATE capability_executions SET execution_metadata_json=? WHERE request_id='run:1'",[json.dumps({"backend":"claude","usage_state":"UNKNOWN","estimated_cost_usd":None})])
    store.conn.commit()
    reader=CharacterExecutionReader(store,tmp_path/"details")
    first=reader.list("alice",limit=1)
    second=reader.list("alice",limit=1,cursor=first["next_cursor"])
    third=reader.list("alice",limit=1,cursor=second["next_cursor"])
    assert len({p["items"][0]["request_id"] for p in (first,second,third)})==3
    assert third["next_cursor"] is None
    assert first["summary"]["unknown_usage_records"]==1
    assert reader.detail("alice","run:1")["execution"]["estimated_cost_usd"] is None
    store.close()


def test_old_database_read_does_not_migrate_or_fake_known_usage(tmp_path):
    from character_memory.character_life import CharacterExecutionReader
    store=SQLiteStore(tmp_path/"old.db")
    reader=CharacterExecutionReader(store,tmp_path/"details")
    assert reader.list("a")["items"]==[]
    assert not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='capability_executions'").fetchone()
    store.close()


def test_detail_path_cannot_escape_execution_directory(tmp_path):
    from character_memory.character_life import CharacterExecutionReader
    store=SQLiteStore(tmp_path/"local.db")
    req=replace(request(),constraints={**request().constraints,**limits()})
    CapabilityExecutor(store,Observer(),claude_runner=Runner()).execute(req)
    store.conn.execute("UPDATE capability_executions SET execution_metadata_json=?",[json.dumps({"backend":"claude","detail_path":"../secret.json"})])
    store.conn.commit()
    (tmp_path/"secret.json").write_text('{"assistant_response":"SECRET"}')
    detail=CharacterExecutionReader(store,tmp_path/"details").detail("alice",req.request_id)
    assert "SECRET" not in str(detail) and detail["details_state"]=="UNAVAILABLE"
    store.close()


def test_http_execution_details_are_scoped_and_readonly(tmp_path):
    store=SQLiteStore(tmp_path/"local.db")
    CapabilityExecutor(store,Observer()).execute(request())
    app=FastAPI()
    def ensure(cid):
        if cid not in {"alice","bob"}:raise HTTPException(404,"unknown")
    attach_character_life_routes(app,SimpleNamespace(read_store=store,ensure_character=ensure,
        web_dir=Path(__file__).parents[1]/"src/character_memory/web",settings=SimpleNamespace(db_path=tmp_path/"local.db")))
    with TestClient(app) as client:
        assert client.get("/v1/characters/alice/executions").json()["read_only"]
        assert client.get("/v1/characters/alice/executions/run:1").status_code==200
        assert client.get("/v1/characters/bob/executions/run:1").status_code==404
        assert client.get("/v1/characters/alice/executions?limit=900").status_code==422
        import base64
        invalid=base64.urlsafe_b64encode(json.dumps(["not-a-time","run:1"]).encode()).decode()
        assert client.get("/v1/characters/alice/executions",params={"cursor":invalid}).status_code==422
        assert client.post("/v1/characters/alice/executions/run:1").status_code==405
    store.close()


def test_execution_paging_orders_absolute_time_and_exact_ties(tmp_path):
    from character_memory.character_life import CharacterExecutionReader
    store=SQLiteStore(tmp_path/'time.db')
    CapabilityExecutor(store,Observer())
    entries=[('earlier','2026-10-12T09:00:00+08:00'),('later','2026-10-12T08:30:00+00:00'),
             ('tie-z','2026-10-12T16:30:00+08:00'),('micro-later','2026-10-12T08:30:00.000001+00:00')]
    with store.transaction():
        for identifier,stamp in entries:
            store.conn.execute("INSERT INTO capability_executions(request_id,character_id,opportunity_id,request_json,status,started_at) VALUES(?,?,?,'{}','STARTED',?)",[identifier,'alice',identifier,stamp])
    reader=CharacterExecutionReader(store,tmp_path/'details')
    ids=[];cursor=''
    for _ in entries:
        page=reader.list('alice',limit=1,cursor=cursor)
        ids.append(page['items'][0]['request_id']);cursor=page['next_cursor']
    assert ids==['micro-later','tie-z','later','earlier']
    assert cursor is None
    # Previously issued opaque cursors used the same original timestamp/id pair.
    import base64
    legacy=base64.urlsafe_b64encode(json.dumps(['2026-10-12T16:30:00+08:00','tie-z']).encode()).decode()
    assert [i['request_id'] for i in reader.list('alice',cursor=legacy)['items']]==['later','earlier']
    assert reader.list('bob')['items']==[]
    store.close()
