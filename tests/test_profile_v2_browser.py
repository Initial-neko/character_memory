import os
import re
import socket
import threading
import time
from datetime import datetime, timezone

import pytest

from test_life_conversation import isolated_app


pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="explicit browser run")]


@pytest.fixture
def profile_app(isolated_app):
    import uvicorn

    from character_memory.domain.models import Event, EventType
    from character_memory.space_web import attach_space_routes

    app = isolated_app
    attach_space_routes(app)
    access = app.state.character_memory
    store = access.store()
    character = access.character_profiles()[0]["id"]
    event = store.append_event(Event(
        character_id=character,
        event_type=EventType.USER_MESSAGE,
        event_time=datetime.now(timezone.utc),
        content="浏览器原始事实 <script>window.FORGED=true</script>",
        metadata={"conversation_id": "profile-browser-existing"},
    ))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        yield f"http://127.0.0.1:{port}", character, event, store, access
    finally:
        access.stream_hub.close()
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive()


def prepare_profile(page, base, character=None):
    # Observe the app's lifecycle events before the real deferred scripts run.
    # The readiness marker fires after listeners have handled each event, so
    # tests cannot race the first history load or a startup conversation switch.
    page.add_init_script("""
        document.addEventListener("DOMContentLoaded", () => {
          const cm = window.CM;
          if (!cm) return;
          const emit = cm.emit;
          cm.emit = async function(name, payload) {
            const result = await emit.call(cm, name, payload);
            if (name === "historyLoaded") document.body.dataset.profileHistoryLoaded = "true";
            if (name === "conversationChanged") document.body.dataset.profileConversationChanged = "true";
            return result;
          };
        }, {once:true});
    """)
    page.goto(f"{base}/")
    page.wait_for_function("""
        window.CM && window.CM.state.characters.length > 0 &&
        window.CM.features.profile && document.body.dataset.cmReady === "true" &&
        document.body.dataset.profileHistoryLoaded === "true"
    """)
    page.wait_for_function("document.querySelectorAll('.profile-v2-shell').length === 1")
    if character is not None:
        select_character_from_chat(page, character)


def select_character_from_chat(page, character):
    current = page.evaluate("window.CM.state.characterId")
    if current == character:
        return
    rows = page.locator("#characterList [data-character]")
    target = next(
        (rows.nth(index) for index in range(rows.count())
         if rows.nth(index).get_attribute("data-character") == character),
        None,
    )
    assert target is not None, f"character {character!r} is not visible in the chat sidebar"
    target.click()
    page.wait_for_function(
        "id => window.CM.state.characterId === id && document.body.dataset.profileConversationChanged === 'true'",
        arg=character,
    )


def install_profile(page, base, character):
    prepare_profile(page, base, character)
    assert page.evaluate("window.CM.state.characterId") == character
    page.get_by_role("button", name=re.compile("人物主页")).click()


def test_profile_renders_real_bounded_records_safely_and_opens_life(page, profile_app):
    from playwright.sync_api import expect

    base, character, event, store, access = profile_app
    # Historical text posts remain readable after the new image publication gate.
    access.space_repository.create_post(character, "真实公开动态 <script>window.POST_FORGED=true</script>", datetime.now(timezone.utc))
    before_events = store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    install_profile(page, base, character)

    expect(page.locator(".profile-v2-shell")).to_be_visible()
    expect(page.locator("[data-profile-name]")).to_have_text(access.character_profiles()[0]["name"])
    expect(page.locator("[data-profile-post]").filter(has_text="真实公开动态")).to_be_visible()
    assert page.evaluate("window.POST_FORGED") is None

    page.get_by_role("tab", name="我们的故事").click()
    expect(page.locator("[data-profile-story]").filter(has_text="浏览器原始事实")).to_be_visible()
    assert page.evaluate("window.FORGED") is None
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events

    page.get_by_role("button", name=re.compile("查看共同经历")).last.click()
    expect(page).to_have_url(re.compile(rf"/life\?character={re.escape(character)}&view=relationships$"))
    assert page.request.get(f"{base}/health").json()["runtime_loaded"] is False


def test_profile_chat_button_returns_to_direct_chat_without_sending(page, profile_app):
    from playwright.sync_api import expect

    base, character, event, store, _ = profile_app
    before_events = store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url)))

    install_profile(page, base, character)
    expect(page.locator("body")).to_have_class(re.compile(r"profile-mode"))
    expect(page.locator("#messageInput")).to_be_hidden()
    page.get_by_role("button", name="和她聊聊").click()

    expect(page.locator(".profile-v2-shell")).to_be_hidden()
    expect(page.locator("#messageInput")).to_be_visible()
    assert page.evaluate("window.CM.state.characterId") == character
    assert not [request for method, request in requests if method == "POST" and request.endswith("/v1/chat/messages")]
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events
    assert page.request.get(f"{base}/health").json()["runtime_loaded"] is False


def test_profile_request_epoch_ignores_older_character_response(page, profile_app):
    from playwright.sync_api import expect

    base, first, event, store, access = profile_app
    profiles = access.character_profiles()
    second_profile = next((item for item in profiles if item["id"] != first and not item.get("archived_at")), None)
    if second_profile is None:
        pytest.skip("isolated app needs two active characters for the race check")
    second = second_profile["id"]
    second_name = second_profile["name"]
    access.space_repository.create_post(second, "第二个人物的真实动态", datetime.now(timezone.utc))

    prepare_profile(page, base, first)
    pending = []
    first_posts = re.compile(rf".*/v1/space/posts\?character_id={re.escape(first)}&limit=4$")
    page.route(first_posts, lambda route: pending.append(route))
    with page.expect_request(lambda request: first_posts.search(request.url)):
        page.get_by_role("button", name=re.compile("人物主页")).click()
    deadline = time.monotonic() + 2
    while not pending and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(pending) == 1

    page.evaluate("async id => { await window.CM.switchCharacter(id); }", second)
    expect(page.locator("[data-profile-name]")).to_have_text(second_name)
    expect(page.locator("[data-profile-post]").filter(has_text="第二个人物的真实动态")).to_be_visible()

    pending[0].fulfill(json={"posts": [{
        "id": 990001,
        "character_id": first,
        "content": "旧人物迟到的动态",
        "created_at": "2026-10-14T10:00:00+00:00",
        "media_items": [],
    }]})
    expect(page.locator("[data-profile-name]")).to_have_text(second_name)
    expect(page.locator("[data-profile-post]").filter(has_text="第二个人物的真实动态")).to_be_visible()
    expect(page.locator("[data-profile-post]").filter(has_text="旧人物迟到的动态")).to_have_count(0)
    assert page.request.get(f"{base}/health").json()["runtime_loaded"] is False


def test_profile_request_epoch_ignores_older_profile_resolution(page, profile_app):
    from playwright.sync_api import expect

    base, first, event, store, access = profile_app
    second_profile = next(
        (item for item in access.character_profiles() if item["id"] != first and not item.get("archived_at")),
        None,
    )
    if second_profile is None:
        pytest.skip("isolated app needs two active characters for the profile-resolution race check")
    second = second_profile["id"]
    prepare_profile(page, base, first)

    late_character = "profile-resolution-that-arrives-late"
    pending = []
    active_characters = re.compile(r".*/v1/characters\?include_deferred=true$")
    page.route(active_characters, lambda route: pending.append(route))
    with page.expect_request(lambda request: active_characters.search(request.url)):
        page.evaluate("id => { window.lateProfileOpen = window.CM.features.profile.open(id); }", late_character)
    deadline = time.monotonic() + 2
    while not pending and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(pending) == 1

    page.evaluate("async id => { await window.CM.switchCharacter(id); }", second)
    page.get_by_role("tab", name="认识她").click()
    about = page.locator("[data-profile-panel] .profile-v2-about-facts")
    expect(about.locator("dd").nth(0)).to_have_text(second_profile.get("identity") or "尚未填写")
    expect(about.locator("dd").nth(1)).to_have_text(second_profile.get("tagline") or "尚未填写")

    pending[0].fulfill(json={"characters": [{
        "id": late_character,
        "name": "迟到的人物 A",
        "identity": "A 的迟到身份",
        "tagline": "A 的迟到简介",
    }]})
    page.evaluate("async () => await window.lateProfileOpen")
    page.get_by_role("tab", name="认识她").click()
    expect(about.locator("dd").nth(0)).to_have_text(second_profile.get("identity") or "尚未填写")
    expect(about.locator("dd").nth(1)).to_have_text(second_profile.get("tagline") or "尚未填写")
    assert page.evaluate("window.CM.state.characterId") == second

    page.unroute(active_characters)
    failed_character = "profile-resolution-that-fails-late"
    pending_failure = []
    page.route(active_characters, lambda route: pending_failure.append(route))
    with page.expect_request(lambda request: active_characters.search(request.url)):
        page.evaluate("id => { window.failedProfileOpen = window.CM.features.profile.open(id); }", failed_character)
    deadline = time.monotonic() + 2
    while not pending_failure and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(pending_failure) == 1

    page.evaluate("async id => { await window.CM.switchCharacter(id); }", first)
    page.evaluate("async id => { await window.CM.switchCharacter(id); }", second)
    pending_failure[0].abort()
    page.evaluate("async () => await window.failedProfileOpen")
    page.get_by_role("tab", name="认识她").click()
    expect(about.locator("dd").nth(0)).to_have_text(second_profile.get("identity") or "尚未填写")
    expect(about.locator("dd").nth(1)).to_have_text(second_profile.get("tagline") or "尚未填写")
    expect(page.locator("[data-profile-status]")).to_have_text("")
    assert page.request.get(f"{base}/health").json()["runtime_loaded"] is False


@pytest.mark.parametrize("over_soft", [False, True])
def test_profile_chat_opens_direct_for_deferred_character_and_survives_reload(page, profile_app, over_soft):
    from character_memory.config import set_character_direct_pending

    base, _, _, _, access = profile_app
    deferred = access.character_profiles()[1]["id"]
    set_character_direct_pending(access.settings, deferred, True)

    prepare_profile(page, base)
    page.evaluate("id => window.CM.features.profile.open(id)", deferred)
    page.wait_for_function("document.body.classList.contains('profile-mode')")
    if over_soft:
        page.evaluate("window.CM.state.characterCapacity.softLimit = 1")
        dialogs = []
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
        with page.expect_request(lambda request: "/open-direct?confirm_over_soft_limit=true" in request.url):
            page.locator("[data-profile-chat]").click()
        assert len(dialogs) == 1
    else:
        page.locator("[data-profile-chat]").click()

    page.wait_for_function("id => window.CM.state.characterId === id", arg=deferred)
    page.wait_for_function(
        "id => [...document.querySelectorAll('#characterList [data-character]')]"
        ".some(el => el.dataset.character === id)",
        arg=deferred,
    )

    page.reload()
    page.wait_for_function(
        "id => window.CM && window.CM.state.characterId === id && document.body.dataset.cmReady === 'true'",
        arg=deferred,
    )
    assert page.evaluate("window.CM.state.characterId") == deferred
    assert page.evaluate(
        "id => [...document.querySelectorAll('#characterList [data-character]')]"
        ".some(el => el.dataset.character === id)",
        deferred,
    )
