import json
import os
from pathlib import Path
import socket
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

pytestmark=[pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT")!="1",reason="explicit isolated browser run")]


def test_execution_details_escape_text_and_never_write(page, tmp_path):
    import uvicorn
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from character_memory.character_life_web import attach_character_life_routes
    from character_memory.storage.sqlite import SQLiteStore
    from character_memory.domain.models import Event, EventType
    from character_memory.runtime.capability_execution import CapabilityExecutor
    from test_claude_world import Runner, limits
    from test_claude_process import request, Observer
    from dataclasses import replace
    store=SQLiteStore(tmp_path/"browser.db")
    req=replace(request(),constraints={**request().constraints,**limits()})
    CapabilityExecutor(store,Observer(),claude_runner=Runner()).execute(req)
    identifier=str(uuid.uuid4())+".json"
    folder=tmp_path/"claude-executions";folder.mkdir()
    (folder/identifier).write_text(json.dumps({"assistant_response":"<script>window.EXECUTED=true</script>",
        "receipts":[{"url":"https://example.org/source","content_scope":"WEB_EXCERPT","read_characters":32}]}))
    row=store.conn.execute("SELECT execution_metadata_json FROM capability_executions").fetchone()
    meta=json.loads(row[0]);meta.update(detail_path=identifier,tool_calls=1,
        effective_tools=["mcp__character_world__world_lookup"],mcp_servers=[{"name":"character_world","status":"connected"}],cli_version="2.1.295")
    store.conn.execute("UPDATE capability_executions SET execution_metadata_json=?",[json.dumps(meta)])
    store.conn.commit()
    store.append_event(Event(character_id="alice",event_type=EventType.WORLD_OBSERVATION,event_time=datetime.now(timezone.utc),
                            content="隔离来源验收",metadata={"capability_request_id":req.request_id}))
    app=FastAPI();web=Path(__file__).parents[1]/"src/character_memory/web"
    attach_character_life_routes(app,SimpleNamespace(read_store=store,ensure_character=lambda cid:None,web_dir=web,
        settings=SimpleNamespace(db_path=tmp_path/"browser.db")))
    app.mount("/static",StaticFiles(directory=web))
    @app.get("/v1/characters")
    def characters():return {"characters":[{"id":"alice","name":"隔离验收人物"}]}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,log_level="error"))
    thread=threading.Thread(target=server.run)
    before=store.conn.total_changes
    try:
        thread.start()
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        assert server.started
        page.goto(f"http://127.0.0.1:{port}/life")
        page.get_by_role("button",name="查看外部执行").click()
        from playwright.sync_api import expect
        expect(page.locator("#details")).to_contain_text("助手可见回复（非原文）")
        expect(page.locator("#details")).to_contain_text("<script>window.EXECUTED=true</script>")
        assert page.evaluate("window.EXECUTED") is None
        expect(page.get_by_role("link",name="查看来源")).to_have_attribute("href","https://example.org/source")
        expect(page.locator("#details")).to_contain_text("mcp__character_world__world_lookup")
        expect(page.locator("#details")).to_contain_text("character_world: connected")
        expect(page.locator("#details")).to_contain_text("工具调用 1 次")
        assert store.conn.total_changes==before
    finally:
        server.should_exit=True
        thread.join(timeout=10)
        assert not thread.is_alive()
        store.close()
