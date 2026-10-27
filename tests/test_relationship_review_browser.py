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


def test_directional_cognition_shows_evidence_and_corrects_at_desktop_and_phone(page, tmp_path):
    from playwright.sync_api import expect
    state = {"corrected": False}
    posts = []
    def route(request):
        url = request.request.url
        method = request.request.method
        if "/static/" in url:
            name = url.split("/static/")[1]
            request.fulfill(body=(WEB / name).read_bytes(), content_type=__import__("mimetypes").guess_type(name)[0] or "application/octet-stream")
        elif method == "POST" and "/v1/characters/a/memories/11/correct" in url:
            posts.append((url, request.request.post_data_json))
            state["corrected"] = True
            request.fulfill(json={"memory": {"id": 12, "content": "更正后的星灯认知"}})
        elif "/v1/characters?" in url:
            request.fulfill(json={"characters": [{"id": "a", "name": "莉娅"}, {"id": "b", "name": "芙露"}]})
        elif "/relationship-review?" in url:
            content = "更正后的星灯认知" if state["corrected"] else "莉娅记得芙露说过星灯已经修好了"
            request.fulfill(json={
                "character_id": "a", "peer": "b",
                "window": {"start": "2026-10-08T00:00:00Z", "end": "2026-10-14T12:00:00Z"},
                "interactions": [], "pending_intents": [], "interactions_has_more": False,
                "pending_has_more": False,
                "cognition": {
                    "direction": "a->b", "character_id": "a", "peer": "b", "read_only": True,
                    "asserted_count": 1,
                    "cognitions": [
                        {"memory": {"id": 11, "content": content, "memory_type": "EPISODIC", "importance": 0.8,
                                    "event_time": "2026-10-13T12:00:00Z", "active": True, "superseded_by": None},
                         "status": "ACTIVE", "reason": None, "asserted": True, "attribution": "TRACE_INPUT",
                         "evidence": [{"kind": "CONVERSATION_EVENT", "id": 21, "conversation_id": "g",
                                       "event_type": "CHARACTER_MESSAGE", "actor_type": "CHARACTER", "actor_id": "b",
                                       "time": "2026-10-13T11:59:00Z", "content": "芙露说星灯已经修好了",
                                       "role": "WITNESSED_INPUT"}]},
                        {"memory": {"id": 9, "content": "芙露的旧说法", "memory_type": "EPISODIC", "importance": 0.6,
                                    "event_time": "2026-10-12T12:00:00Z", "active": True, "superseded_by": None},
                         "status": "INVALIDATED", "reason": "SOURCE_WITHDRAWN", "asserted": False,
                         "attribution": "WITHDRAWN_INPUT", "evidence": []},
                    ],
                    "open_items": [],
                    "change_history": [{"kind": "MEMORY_CORRECTED", "memory_id": 9, "at": "2026-10-13T12:00:00Z",
                                        "to_memory_id": 11, "to_content": content}],
                    "boundaries": [{"code": "DIRECTIONAL", "text": "A 对 B 与 B 对 A 分开计算，不能从一方推断另一方。"},
                                   {"code": "WITNESSED_ONLY", "text": "同群不等于互动；只有实际看到对方消息才作为依据。"}],
                    "counts": {"active": 1, "invalidated": 1, "superseded": 0, "forgotten": 0,
                               "simulated_excluded": 0, "truncated": False},
                },
            })
        elif "/life?" in url:
            request.fulfill(json={"items": [], "next_cursor": None, "undated_count": 0})
        else:
            request.fulfill(body=(WEB / "life.html").read_text(encoding="utf-8"), content_type="text/html")
    page.route("http://relationship.test/**", route)
    page.goto("http://relationship.test/life")
    page.locator('[data-view="relationships"]').click()
    page.locator("#peer").select_option("b")
    area = page.locator("#relationshipReview")
    expect(area).to_contain_text("莉娅 对 芙露 的认知")
    expect(area).to_contain_text("当前断言 1 条")
    expect(area).to_contain_text("来源已失效")
    expect(area).to_contain_text("A 对 B 与 B 对 A 分开计算")
    area.locator("summary").first.click()
    expect(area).to_contain_text("芙露说星灯已经修好了")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.screenshot(path=str(tmp_path / "cognition-desktop.png"), full_page=True)
    page.once("dialog", lambda dialog: dialog.accept("更正后的星灯认知"))
    area.locator('[data-cognition-action="correct"]').click()
    expect(area).to_contain_text("更正后的星灯认知")
    assert posts and posts[0][1]["content"] == "更正后的星灯认知"

    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    page.locator('[data-view="relationships"]').click()
    page.locator("#peer").select_option("b")
    area = page.locator("#relationshipReview")
    expect(area).to_contain_text("莉娅 对 芙露 的认知")
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1
    page.screenshot(path=str(tmp_path / "cognition-phone.png"), full_page=True)

