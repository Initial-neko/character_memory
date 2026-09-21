from __future__ import annotations

import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen

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
def wake_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("p0-17c-browser")
    port = _free_port()
    env = os.environ.copy()
    env["CHARACTER_MEMORY_E2E_DB"] = str(root / "browser.db")
    env["CHARACTER_MEMORY_E2E_MEDIA"] = str(root / "media")
    persona_root = root / "personas"
    shutil.copytree(ROOT / "personas", persona_root)
    env["CHARACTER_MEMORY_E2E_PERSONA_ROOT"] = str(persona_root)
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


def test_manual_wake_uses_time_tick_and_existing_direct_sse(page, wake_server):
    page.set_default_timeout(10000)
    page.goto(wake_server, wait_until="domcontentloaded")
    expect(page.locator("#characterList .character-item").first).to_be_visible()
    expect(page.locator("#topbarMoreButton")).to_be_visible()
    page.locator("#topbarMoreButton").click()
    expect(page.locator("#wakeButton")).to_be_visible()
    page.wait_for_function("() => Boolean(CM.state.directStream && CM.state.directStream.readyState === EventSource.OPEN)")

    page.locator("#wakeButton").click()
    expect(page.locator(".message-row.assistant .bubble", has_text="E2E wake reply")).to_be_visible()
    expect(page.locator("#wakeButton")).to_be_enabled()


def _sidebar_width(page) -> float:
    box = page.locator(".sidebar").bounding_box()
    assert box is not None
    return float(box["width"])


def test_sidebar_shell_compact_rail_persists_and_header_stays_clear(page, wake_server):
    page.set_default_timeout(10000)
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(wake_server, wait_until="domcontentloaded")
    expect(page.locator("#characterList .character-item").first).to_be_visible()

    assert 270 <= _sidebar_width(page) <= 290
    assert page.locator("#topbarMenu #characterSpaceButton").count() == 1
    assert page.locator("#topbarMenu #intentButton").count() == 1

    identity_right, actions_left = page.evaluate(
        """() => {
            const identity = document.getElementById("characterIdentity").getBoundingClientRect();
            const actions = document.querySelector(".topbar-actions").getBoundingClientRect();
            return [identity.right, actions.left];
        }"""
    )
    assert identity_right <= actions_left + 1

    page.locator("#sidebarCollapseButton").click()
    assert 66 <= _sidebar_width(page) <= 70
    expect(page.locator(".space-nav-copy")).not_to_be_visible()
    visible_characters = page.locator("#characterList .character-item:visible").count()
    assert 1 <= visible_characters <= 6
    assert page.locator("#characterList .character-tagline:visible").count() == 0

    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#characterList .character-item").first).to_be_visible()
    assert 66 <= _sidebar_width(page) <= 70

    page.locator("#sidebarCollapseButton").click()
    assert 270 <= _sidebar_width(page) <= 290

    page.set_viewport_size({"width": 1366, "height": 768})
    identity_right, actions_left, scroll_width, client_width = page.evaluate(
        """() => {
            const identity = document.getElementById("characterIdentity").getBoundingClientRect();
            const actions = document.querySelector(".topbar-actions").getBoundingClientRect();
            return [identity.right, actions.left, document.documentElement.scrollWidth, document.documentElement.clientWidth];
        }"""
    )
    assert identity_right <= actions_left + 1
    assert scroll_width <= client_width + 1


def test_sidebar_mobile_uses_drawer_not_avatar_rail(page, wake_server):
    page.set_default_timeout(10000)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(wake_server, wait_until="domcontentloaded")
    expect(page.locator("#sidebarMobileButton")).to_be_visible()

    before = page.locator(".sidebar").bounding_box()
    assert before is not None and before["x"] < 0
    expect(page.locator("#sidebarCollapseButton")).not_to_be_visible()

    page.locator("#sidebarMobileButton").click()
    after = page.locator(".sidebar").bounding_box()
    assert after is not None and after["x"] >= -1
    expect(page.locator("#characterList .character-copy").first).to_be_visible()

    page.locator("#characterList .character-item").first.click()
    page.wait_for_timeout(100)
    closed = page.locator(".sidebar").bounding_box()
    assert closed is not None and closed["x"] < 0


def test_character_archive_confirmation_and_restore_round_trip(page, wake_server):
    page.set_default_timeout(10000)
    page.set_viewport_size({"width": 1366, "height": 768})
    page.goto(wake_server, wait_until="domcontentloaded")
    rows = page.locator("#characterList [data-character-row]")
    expect(rows.first).to_be_visible()
    before_count = rows.count()
    assert before_count >= 2

    target = rows.nth(1)
    character_id = target.get_attribute("data-character-row")
    assert character_id
    target.hover()
    target.locator("[data-character-more]").click()
    target.locator("[data-character-archive]").click()

    page.wait_for_function("() => document.getElementById('drawer').classList.contains('open')")
    expect(page.locator("[data-character-archive-confirm]")).to_be_visible()
    expect(page.locator("#drawerBody")).to_contain_text("聊天记录")
    expect(page.locator("#drawerBody")).to_contain_text("空间动态")

    page.locator("[data-character-archive-confirm]").click()
    expect(page.locator(f'#characterList [data-character-row="{character_id}"]')).to_have_count(0)

    page.locator(".character-archive-entry").click()
    expect(page.locator("#drawerTitle")).to_have_text("已归档人物")
    restore = page.locator(f'[data-character-restore="{character_id}"]')
    expect(restore).to_be_visible()
    restore.click()
    expect(page.locator(f'#characterList [data-character-row="{character_id}"]')).to_have_count(1)
    assert page.locator("#characterList [data-character-row]").count() == before_count
