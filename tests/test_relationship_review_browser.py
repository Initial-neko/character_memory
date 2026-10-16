import os
from pathlib import Path

import pytest


pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="explicit isolated browser validation")]
WEB = Path(__file__).parents[1] / "src/character_memory/web"


def test_relationship_review_source_plans_peer_and_character_switch(page):
    from playwright.sync_api import expect
    requests = []
    def route(request):
        url = request.request.url
        requests.append((request.request.method, url))
        if "/static/" in url:
            name = url.split("/static/")[1]
            request.fulfill(body=(WEB / name).read_bytes(), content_type=__import__("mimetypes").guess_type(name)[0] or "application/octet-stream")
        elif "/v1/characters?" in url:
            request.fulfill(json={"characters": [{"id": "a", "name": "莉娅"}, {"id": "b", "name": "芙露"}]})
        elif "/relationship-review?" in url:
            character = url.split("/characters/")[1].split("/")[0]
            peer = "user" if "peer=user" in url else "b"
            request.fulfill(json={
                "peer": peer, "window": {"start": "2026-10-08T00:00:00Z", "end": "2026-10-14T12:00:00Z"},
                "interactions": [{"key": "events:7", "time": "2026-10-13T12:00:00Z", "channel": "DIRECT", "content": f"{character}的真实星灯互动 <script>bad()</script>", "source": {"kind": "events", "id": 7}}] if peer == "user" else [],
                "pending_intents": [{"content": "稍后问用户星灯", "source": {"kind": "intent", "id": 3, "event_id": 7}, "source_known": True, "earliest_at": "2026-10-15T00:00:00Z", "expires_at": "2026-10-16T00:00:00Z"}] if peer == "user" else [],
                "interactions_has_more": False, "pending_has_more": False,
            })
        elif "/life?" in url:
            request.fulfill(json={"items": [], "next_cursor": None, "undated_count": 0})
        else:
            request.fulfill(body=(WEB / "life.html").read_text(encoding="utf-8"), content_type="text/html")
    page.route("http://relationship.test/**", route)
    page.goto("http://relationship.test/life")
    page.locator('[data-view="relationships"]').click()
    page.locator("#peer").select_option("user")
    area = page.locator("#relationshipReview")
    expect(area).to_contain_text("a的真实星灯互动")
    expect(area).to_contain_text("尚未执行")
    expect(area).to_contain_text("群聊文字邀请不自动解析成计划")
    assert "life_character=a" in area.locator("a").get_attribute("href")
    assert "life_key=events%3A7" in area.locator("a").get_attribute("href")
    assert area.locator("script").count() == 0
    page.locator("#peer").select_option("b")
    expect(area).to_contain_text("没有向该人物的可执行计划接口")
    expect(area).not_to_contain_text("稍后问用户星灯")
    page.locator("#peer").select_option("user")
    page.locator("#character").select_option("b")
    expect(area).to_contain_text("b的真实星灯互动")
    expect(area).not_to_contain_text("a的真实星灯互动")
    assert all(method == "GET" for method, _ in requests)
    page.locator('[data-view="life"]').click()
    expect(area).to_be_hidden()


def test_late_relationship_response_cannot_restore_old_peer(page):
    from playwright.sync_api import expect
    html = (WEB / "life.html").read_text(encoding="utf-8")
    page.route("http://relationship.test/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    page.add_init_script("""window.fetch = async url => {
        const value=String(url);
        if(value.includes('/v1/characters?')) return {ok:true,json:async()=>({characters:[{id:'a',name:'A'},{id:'b',name:'B'}]})};
        if(value.includes('relationship-review')) {
            if(value.includes('peer=user')) return await new Promise(resolve=>window.lateReview=resolve);
            return {ok:true,json:async()=>({peer:'b',window:{start:'2026-10-08',end:'2026-10-14'},interactions:[],pending_intents:[]})};
        }
        return {ok:true,json:async()=>({items:[],next_cursor:null})};
    };""")
    page.goto("http://relationship.test/life")
    page.add_script_tag(content=(WEB / "life.js").read_text(encoding="utf-8"))
    page.locator('[data-view="relationships"]').click()
    page.locator("#peer").select_option("user")
    page.wait_for_function("()=>Boolean(window.lateReview)")
    page.locator("#peer").select_option("b")
    expect(page.locator("#relationshipReview")).to_contain_text("与B的近期回顾")
    page.evaluate("window.lateReview({ok:true,json:async()=>({peer:'user',window:{start:'2026-10-08',end:'2026-10-14'},interactions:[],pending_intents:[{content:'旧对象私人计划'}]})})")
    expect(page.locator("#relationshipReview")).not_to_contain_text("旧对象私人计划")
