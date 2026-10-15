"""Browser coverage for existing World candidate actions using a test-owned DB."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import socket
import threading
import time

import pytest

from test_life_conversation import isolated_app


pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="explicit browser run"),
]


def _candidate_draft(name: str) -> dict:
    return {
        "name": name,
        "age": 23,
        "identity": "夜间电器维修店的店员",
        "tagline": "会把坏掉的小东西拆开看看",
        "description": f"{name} 喜欢修理旧电子设备，观察细，熟悉以后会聊很多冷门零件。",
        "personality": ["安静", "好奇", "有自己的判断"],
        "conversation": "自然短句。",
        "expression": "很少刷表情。",
        "questions": "真的好奇才问。",
        "silence": "没话时可以沉默。",
        "initiative": "遇到旧设备会主动分享。",
        "disagreement": "不同意会直接说理由。",
        "care": "记住具体小事。",
        "boundaries": ["不迎合", "关系慢慢形成"],
    }


@pytest.fixture
def live_world_candidates(isolated_app):
    import uvicorn

    from character_memory.encounter_web import attach_encounter_routes
    from character_memory.encounter_store import EncounterRepository

    app = isolated_app
    access = app.state.character_memory
    attach_encounter_routes(app)

    created_profiles = []

    def fake_creator(draft, *_args, **_kwargs):
        """Keep accept-path effects inside this fixture's temporary SQLite DB."""
        profile = {"id": "world-browser-accepted", "name": draft.name}
        created_profiles.append(profile)
        return profile

    access.create_character_from_draft = fake_creator
    active_count = sum(1 for item in access.character_profiles() if "archived_at" not in item)
    # Exercise the existing confirmation UI without touching real character files.
    access.soft_active_characters = active_count
    access.max_active_characters = max(20, active_count + 1)

    repository = access.encounter_repository
    candidate_ids = {}
    for key, name in (("chat", "Browser Chat Guest"), ("accept", "Browser Accept Guest"), ("dismiss", "Browser Dismiss Guest")):
        candidate = repository.create_candidate(
            source_type="GENERATED",
            draft=_candidate_draft(name),
            encounter_hook=f"{name} 的测试候选介绍。",
            opening_message=f"你好，我是{name}。",
            source_query="",
            source_urls=[f"https://example.invalid/{key}"],
            source_domains=["example.invalid"],
            now=datetime.now(timezone.utc),
        )
        candidate_ids[key] = int(candidate["id"])

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, name="world-candidate-browser-test", daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("World candidate browser server exited before startup")
            time.sleep(0.05)
        assert server.started
        yield f"http://127.0.0.1:{port}", candidate_ids, repository, access, created_profiles
    finally:
        access.stream_hub.close()
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive(), "World candidate browser server did not stop"


def test_world_candidate_chat_accept_confirmation_and_dismiss(page, live_world_candidates):
    from playwright.sync_api import expect

    base, candidate_ids, repository, access, created_profiles = live_world_candidates
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url)))
    page.goto(base + "/?view=world", wait_until="domcontentloaded")
    page.wait_for_function("document.body.dataset.cmReady === 'true'")
    world = page.locator(".cm-world-page")
    expect(world).to_be_visible()

    chat_card = world.locator(f'[data-encounter-id="{candidate_ids["chat"]}"]')
    expect(chat_card).to_be_visible()
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/v1/encounters/{candidate_ids['chat']}/seen")) as seen_response:
        chat_card.get_by_role("button", name="打个招呼").click()
    assert seen_response.value.ok
    expect(chat_card.locator("[data-encounter-panel]")).to_be_visible()
    chat_form = chat_card.locator("[data-encounter-form]")
    chat_form.locator("input").fill("我想看看修理店附近有什么。");
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/v1/encounters/{candidate_ids['chat']}/messages")) as message_response:
        chat_form.get_by_role("button", name="发送").click()
    sent = message_response.value.json()
    assert message_response.value.ok and sent["reply_pending"] is True
    assert [item["role"] for item in sent["messages"]] == ["USER"]
    expect(chat_card.locator("[data-encounter-panel]")).to_be_visible()
    expect(chat_card.get_by_text("我想看看修理店附近有什么。", exact=True)).to_be_visible()
    assert repository.list_messages(candidate_ids["chat"])[0]["content"] == "我想看看修理店附近有什么。"
    assert access.settings.api_key == ""
    assert page.request.get(base + "/health").json()["runtime_loaded"] is False


    accept_card = world.locator(f'[data-encounter-id="{candidate_ids["accept"]}"]')
    expect(accept_card).to_be_visible()
    accept_button = accept_card.get_by_role("button", name="留下 TA")
    dialogs = []
    page.once("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    accept_button.click()
    expect(accept_button).to_be_enabled()
    assert dialogs and "角色" in dialogs[0]
    assert not [url for method, url in requests if method == "POST" and f"/v1/encounters/{candidate_ids['accept']}/accept" in url]

    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.request.method == "POST" and f"/v1/encounters/{candidate_ids['accept']}/accept?confirm_over_soft_limit=true" in response.url) as accepted_response:
        accept_button.click()
    assert accepted_response.value.ok
    accepted = page.request.get(base + "/v1/encounters", params={"limit": 50, "include_closed": "true"}).json()
    accepted_candidate = next(item for item in accepted["encounters"] if item["id"] == candidate_ids["accept"])
    assert accepted_candidate["status"] == "ACCEPTED"
    assert accepted_candidate["accepted_character_id"] == "world-browser-accepted"
    assert created_profiles == [{"id": "world-browser-accepted", "name": "Browser Accept Guest"}]

    dismiss_card = world.locator(f'[data-encounter-id="{candidate_ids["dismiss"]}"]')
    expect(dismiss_card).to_be_visible()
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/v1/encounters/{candidate_ids['dismiss']}/dismiss")) as dismissed_response:
        dismiss_card.get_by_role("button", name="错过").click()
    assert dismissed_response.value.ok
    expect(world.locator(f'[data-encounter-id="{candidate_ids["dismiss"]}"]')).to_have_count(0)
    closed = page.request.get(base + "/v1/encounters", params={"limit": 50, "include_closed": "true"}).json()
    dismissed_candidate = next(item for item in closed["encounters"] if item["id"] == candidate_ids["dismiss"])
    assert dismissed_candidate["status"] == "DISMISSED"
    assert not any(method == "POST" and "/v1/encounters/dev/" in url for method, url in requests)


def test_world_portrait_button_persists_real_image_and_refresh_is_free(page, live_world_candidates, tmp_path):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from playwright.sync_api import expect
    from character_memory.avatars import AvatarStore
    from character_memory.visual_generation import ImageGenerationResult
    base, ids, _, access, _ = live_world_candidates
    image = io.BytesIO()
    Image.new('RGB', (300, 400), '#a08bc9').save(image, format='PNG')
    calls = []
    def generate(request):
        calls.append(request)
        return ImageGenerationResult(provider='test', model='test', payload=image.getvalue(), mime_type='image/png')
    original_store = access.services.avatar_store
    original_providers = access.services.image_generation_providers
    access.services.avatar_store = AvatarStore(tmp_path / 'guest-portraits')
    access.services.image_generation_providers = {'test': SimpleNamespace(configured=lambda: True,
        available=lambda: True, generate=generate)}
    access.settings.image_generation_provider = 'test'
    access.settings.encounter_portraits_enabled = True
    from character_memory.encounter import EncounterService
    access.encounter_service.portraits = EncounterService(access, access.encounter_repository).portraits
    try:
        page.goto(base + '/?view=world')
        card = page.locator(f'[data-encounter-id="{ids["chat"]}"]')
        expect(card).to_be_visible()
        assert not calls
        card.get_by_role('button', name='生成肖像').click()
        portrait = card.locator('.encounter-portrait > img')
        expect(portrait).to_be_visible()
        page.wait_for_function("""() => {const img=document.querySelector('.encounter-portrait > img'); return img?.naturalWidth>0;}""")
        url = portrait.get_attribute('src')
        assert page.request.get(base + url).headers['content-type'] == 'image/png'
        assert len(calls) == 1
        assert page.request.post(f'{base}/v1/encounters/{ids["chat"]}/portrait').ok
        page.reload()
        expect(card.locator('.encounter-portrait > img')).to_have_attribute('src', url)
        assert len(calls) == 1
        assert page.request.post(base + '/v1/encounters/9999/portrait').status == 404
    finally:
        access.services.avatar_store.close()
        access.services.avatar_store = original_store
        access.services.image_generation_providers = original_providers
