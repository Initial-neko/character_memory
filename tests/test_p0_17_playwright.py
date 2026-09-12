from __future__ import annotations

import base64
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from uuid import uuid4

import pytest

if os.getenv("RUN_PLAYWRIGHT") == "1":
    from playwright.sync_api import expect
else:
    expect = None


pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="browser smoke runs in dedicated CI job"),
]

ROOT = Path(__file__).resolve().parents[1]
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def web_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("p0-17-browser")
    port = _free_port()
    env = os.environ.copy()
    env["CHARACTER_MEMORY_E2E_DB"] = str(root / "browser.db")
    env["CHARACTER_MEMORY_E2E_MEDIA"] = str(root / "media")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "e2e_app:app",
            "--app-dir",
            str(ROOT / "tests"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"E2E server exited early:\n{output}")
        try:
            with urlopen(f"{base}/health", timeout=0.5) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        process.terminate()
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError(f"E2E server did not become ready:\n{output}")

    yield base

    process.terminate()
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _open(page, web_server):
    page.set_default_timeout(10000)
    page.goto(web_server, wait_until="domcontentloaded")
    expect(page.locator("#characterList .character-item").first).to_be_visible()
    expect(page.locator("#messageInput")).to_be_visible()


def test_browser_smoke_1_webui_and_fixed_composer(page, web_server):
    _open(page, web_server)
    position = page.locator(".composer-wrap").evaluate("el => getComputedStyle(el).position")
    assert position == "fixed"
    expect(page.locator("#sendButton")).to_be_enabled()
    expect(page.locator("#wakeButton")).to_be_visible()


def test_browser_smoke_2_direct_send_and_sse_reply(page, web_server):
    _open(page, web_server)
    marker = f"direct-{uuid4().hex[:8]}"
    page.locator("#messageInput").fill(marker)
    page.locator("#sendButton").click()

    expect(page.locator(".message-row.user .bubble", has_text=marker)).to_be_visible()
    expect(page.locator(".message-row.assistant .bubble", has_text=f"E2E reply: {marker}")).to_be_visible()
    expect(page.locator("#messageInput")).to_be_enabled()


def test_browser_smoke_3_sse_reconnect_reconciles_durable_history_without_duplicates(page, web_server):
    _open(page, web_server)
    marker = f"reconnect-{uuid4().hex[:8]}"

    # Let the initial EventSource open/reconcile finish, then close it. This
    # ensures the message below truly lands while the page has no live stream.
    page.evaluate("CM.closeDirectStream()")
    page.wait_for_timeout(500)
    result = page.evaluate(
        """async marker => {
          const characterId = CM.state.characterId;
          const conversationId = CM.conversationIdFor(characterId);
          const response = await fetch('/v1/chat/messages', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({character_id:characterId, conversation_id:conversationId, message:marker})
          });
          return {status:response.status, characterId, conversationId};
        }""",
        marker,
    )
    assert result["status"] == 202
    page.wait_for_timeout(1200)
    assert page.locator(".message-row.user .bubble", has_text=marker).count() == 0

    page.evaluate("CM.connectDirectStream()")
    expect(page.locator(".message-row.user .bubble", has_text=marker)).to_have_count(1)
    expect(page.locator(".message-row.assistant .bubble", has_text=f"E2E reply: {marker}")).to_have_count(1)
    page.wait_for_timeout(300)
    assert page.locator(".message-row.user .bubble", has_text=marker).count() == 1
    assert page.locator(".message-row.assistant .bubble", has_text=f"E2E reply: {marker}").count() == 1


def test_browser_smoke_4_group_mention_progressive_reply_and_live_composer(page, web_server):
    _open(page, web_server)
    marker = f"group-{uuid4().hex[:8]}"
    group = page.evaluate(
        """async marker => {
          const characters = (await (await fetch('/v1/characters')).json()).characters;
          const members = characters.slice(0, 2);
          const response = await fetch('/v1/groups', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({name:`Smoke ${marker}`, member_ids:members.map(item => item.id)})
          });
          const data = await response.json();
          return {group:data.group, target:members[1]};
        }""",
        marker,
    )
    page.reload(wait_until="domcontentloaded")
    selector = f'[data-group="{group["group"]["id"]}"]'
    expect(page.locator(selector)).to_be_visible()
    page.locator(selector).click()
    expect(page.locator("#characterName")).to_have_text(group["group"]["name"])

    page.locator("#messageInput").fill("@")
    option = page.locator(".mention-option", has_text=f'@{group["target"]["name"]}')
    expect(option).to_be_visible()
    option.click()
    current = page.locator("#messageInput").input_value()
    page.locator("#messageInput").fill(f"{current}{marker}")
    page.locator("#sendButton").click()

    expect(page.locator("#messageInput")).to_be_enabled()
    expect(page.locator(".message-row.user .bubble", has_text=marker)).to_be_visible()
    expect(page.locator(".message-row.assistant.group-assistant")).to_have_count(2)
    expect(page.locator(".group-speaker-name").first).to_be_visible()


def test_browser_smoke_5_image_uses_vision_path_and_renders_reply(page, web_server):
    _open(page, web_server)
    marker = f"image-{uuid4().hex[:8]}"
    page.locator(".image-file-input").set_input_files(
        {"name":"tiny.png", "mimeType":"image/png", "buffer":PNG_1X1}
    )
    expect(page.locator("#imageCaption")).to_be_visible()
    page.locator("#imageCaption").fill(marker)
    page.locator("[data-image-send]").click()

    expect(page.locator(".message-row.user .image-bubble img")).to_be_visible()
    expect(page.locator(".message-row.assistant .bubble", has_text=f"E2E vision reply: {marker}")).to_be_visible()
