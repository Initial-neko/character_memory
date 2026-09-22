from __future__ import annotations

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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def realtime_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("p0-17b-browser")
    port = _free_port()
    env = os.environ.copy()
    env["CHARACTER_MEMORY_E2E_DB"] = str(root / "browser.db")
    env["CHARACTER_MEMORY_E2E_MEDIA"] = str(root / "media")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "e2e_realtime_app:app",
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


def _open(page, realtime_server):
    page.set_default_timeout(10000)
    page.goto(realtime_server, wait_until="domcontentloaded")
    expect(page.locator("#characterList .character-item").first).to_be_visible()
    expect(page.locator("#messageInput")).to_be_visible()
    page.wait_for_function("() => Boolean(CM.state.directStream && CM.state.directStream.readyState === EventSource.OPEN)")


def test_reconnect_reconciles_durable_direct_history_without_duplicates(page, realtime_server):
    _open(page, realtime_server)
    marker = f"reconnect-{uuid4().hex[:8]}"

    page.evaluate("CM.closeDirectStream()")
    page.wait_for_function("() => CM.state.directStream === null")
    result = page.evaluate(
        """async marker => {
          const characterId = CM.state.characterId;
          const conversationId = CM.conversationIdFor(characterId);
          const response = await fetch('/v1/chat/messages', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({character_id:characterId, conversation_id:conversationId, message:marker})
          });
          return {status:response.status};
        }""",
        marker,
    )
    assert result["status"] == 202
    page.wait_for_timeout(1000)
    assert page.locator(".message-row.user .bubble", has_text=marker).count() == 0

    page.evaluate("CM.connectDirectStream()")
    expect(page.locator(".message-row.user .bubble", has_text=marker)).to_have_count(1)
    expect(page.locator(".message-row.assistant .bubble", has_text=f"E2E reply: {marker}")).to_have_count(1)
    page.wait_for_timeout(300)
    assert page.locator(".message-row.user .bubble", has_text=marker).count() == 1
    assert page.locator(".message-row.assistant .bubble", has_text=f"E2E reply: {marker}").count() == 1


def test_group_mentions_use_authoritative_group_state(page, realtime_server):
    _open(page, realtime_server)
    marker = f"group-{uuid4().hex[:8]}"
    data = page.evaluate(
        """async marker => {
          const characters = (await (await fetch('/v1/characters')).json()).characters;
          const members = characters.slice(0, 2);
          const response = await fetch('/v1/groups', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({name:`Smoke ${marker}`, member_ids:members.map(item => item.id)})
          });
          const payload = await response.json();
          return {group:payload.group, target:members[1]};
        }""",
        marker,
    )

    # Load the newly-created group once through the authoritative Groups feature.
    await_groups = """async groupId => {
      await CM.features.groups.loadGroups();
      await CM.features.groups.enter(groupId);
      return CM.features.groups.current()?.id;
    }"""
    entered = page.evaluate(await_groups, data["group"]["id"])
    assert entered == data["group"]["id"]
    expect(page.locator("#characterName")).to_have_text(data["group"]["name"])

    page.locator("#messageInput").fill("@")
    option = page.locator(".mention-option", has_text=f'@{data["target"]["name"]}')
    expect(option).to_be_visible()
    option.click()
    current = page.locator("#messageInput").input_value()
    page.locator("#messageInput").fill(f"{current}{marker}")
    page.locator("#sendButton").click()

    expect(page.locator(".message-row.user .bubble", has_text=marker)).to_be_visible()
    expect(page.locator(".message-row.assistant.group-assistant")).to_have_count(2)


def test_autonomous_group_messages_stream_without_a_user_turn(page, realtime_server):
    _open(page, realtime_server)
    marker = f"autonomous-{uuid4().hex[:8]}"
    data = page.evaluate(
        """async marker => {
          const characters = (await (await fetch('/v1/characters')).json()).characters;
          const members = characters.slice(0, 2);
          const response = await fetch('/v1/groups', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({name:`Auto ${marker}`, member_ids:members.map(item => item.id)})
          });
          return {group:(await response.json()).group};
        }""",
        marker,
    )

    entered = page.evaluate(
        """async groupId => {
          await CM.features.groups.loadGroups();
          await CM.features.groups.enter(groupId);
          return CM.features.groups.current()?.id;
        }""",
        data["group"]["id"],
    )
    assert entered == data["group"]["id"]
    expect(page.locator(".message-row.user")).to_have_count(0)
    expect(page.locator(".message-row.assistant.group-assistant")).to_have_count(0)

    result = page.evaluate(
        """async groupId => {
          const response = await fetch('/v1/group-autonomy/opportunity/' + encodeURIComponent(groupId), {
            method:'POST'
          });
          return {status:response.status, payload:await response.json()};
        }""",
        data["group"]["id"],
    )
    assert result["status"] == 200
    assert result["payload"]["status"] == "CHATTED"
    assert result["payload"]["message_count"] == 2

    expect(page.locator(".message-row.user")).to_have_count(0)
    expect(page.locator(".message-row.assistant.group-assistant")).to_have_count(2)
    expect(page.locator(".message-row.assistant.group-assistant", has_text="E2E wake reply")).to_have_count(2)

    # Reload from durable history: hidden GROUP_OPPORTUNITY provenance must not
    # become a visible pseudo-user/system bubble.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => Boolean(CM.features.groups)")
    page.evaluate(
        """async groupId => {
          await CM.features.groups.loadGroups();
          await CM.features.groups.enter(groupId);
        }""",
        data["group"]["id"],
    )
    expect(page.locator(".message-row.user")).to_have_count(0)
    expect(page.locator(".message-row.assistant.group-assistant")).to_have_count(2)
