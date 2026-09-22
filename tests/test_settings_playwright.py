from __future__ import annotations

import os
from pathlib import Path
import socket
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
def settings_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("settings-browser")
    port = _free_port()
    env = os.environ.copy()
    env["CHARACTER_SETTINGS_E2E_ROOT"] = str(root)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "e2e_settings_app:app",
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
            raise RuntimeError(f"Settings E2E server exited early:\n{output}")
        try:
            with urlopen(f"{base}/health", timeout=0.5) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        process.terminate()
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError(f"Settings E2E server did not become ready:\n{output}")

    yield base

    process.terminate()
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _reveal(page, selector: str) -> None:
    """Open the collapsed level group that holds `selector`.

    Fields outside the common level live in a closed <details> on purpose, so a
    test that reaches one has to open the group the way a user does.
    """

    details = page.locator(f"details:has({selector})").first
    if details.get_attribute("open") is None:
        details.locator("summary").first.click()


def test_first_screen_shows_only_the_common_level(page, settings_server):
    page.set_default_timeout(10000)
    page.goto(f"{settings_server}/settings", wait_until="domcontentloaded")

    # Every field that renders outside a group, i.e. what the page opens on.
    on_screen = page.evaluate(
        """() => [...document.querySelectorAll('#settingsSections [data-setting]')]
            .filter(el => el.checkVisibility({contentVisibilityAuto: true, visibilityProperty: true, opacityProperty: true}))
            .map(el => el.dataset.setting)"""
    )
    assert on_screen == [
        "chat_temperature",
        "tts_provider",
        "tts_voice",
        "tts_speed",
        "proactive_wake_enabled",
        "space_autonomy_enabled",
        "space_media_enabled",
        "group_autonomy_enabled",
    ]
    # The other 44 fields are in real groups, closed, and each says what it holds.
    assert page.locator("#settingsSections details[open]").count() == 0
    assert page.locator("#settingsSections details.level-group").count() > 0
    summaries = page.locator("#settingsSections summary").all_inner_texts()
    assert all("高级设置" in text or "诊断设置" in text for text in summaries)
    assert all("项）" in text for text in summaries)


def test_restart_requirement_shows_next_to_the_field_not_only_in_a_toast(page, settings_server):
    page.set_default_timeout(10000)
    page.goto(f"{settings_server}/settings", wait_until="domcontentloaded")

    # A hot field must not claim to need a restart...
    assert page.locator('label[for="setting-tts_provider"] .field-restart').count() == 0
    # ...and a field that does need one says so before it is ever saved.
    _reveal(page, "#setting-chat_model")
    marker = page.locator('label[for="setting-chat_model"] .field-restart')
    expect(marker).to_be_visible()

    page.locator("#setting-chat_model").fill("deepseek-chat")
    page.locator("#saveSettings").click()
    expect(page.locator("#notice")).to_contain_text("chat_model")
    # The toast is transient; the marker is not, and it survives the re-render
    # that saving does.
    _reveal(page, "#setting-chat_model")
    expect(marker).to_be_visible()


def test_tts_provider_switch_updates_voice_device_and_persists_without_full_stack_restart(page, settings_server):
    page.set_default_timeout(10000)
    page.goto(f"{settings_server}/settings", wait_until="domcontentloaded")

    _reveal(page, "#setting-tts_device")
    provider = page.locator("#setting-tts_provider")
    voice = page.locator("#setting-tts_voice")
    device = page.locator("#setting-tts_device")
    expect(provider).to_have_value("kokoro")
    expect(voice).to_have_value("zf_001")
    expect(device).to_have_value("cpu")

    provider.select_option("gsv")
    expect(voice).to_have_value("murasame")
    expect(device).to_have_value("cuda")

    page.locator("#saveSettings").click()
    expect(page.locator("#notice")).to_contain_text("无需重启整个 stack")

    # Re-read from disk/API, not merely DOM state.
    _reveal(page, "#reloadSettings")
    page.locator("#reloadSettings").click()
    expect(provider).to_have_value("gsv")
    expect(voice).to_have_value("murasame")
    expect(device).to_have_value("cuda")


def test_non_hot_local_device_change_is_explicitly_restart_required(page, settings_server):
    page.set_default_timeout(10000)
    page.goto(f"{settings_server}/settings", wait_until="domcontentloaded")

    _reveal(page, "#setting-tts_device")
    provider = page.locator("#setting-tts_provider")
    provider.select_option("kokoro")
    page.locator("#saveSettings").click()
    expect(page.locator("#notice")).to_contain_text("无需重启整个 stack")

    _reveal(page, "#setting-tts_device")
    page.locator("#setting-tts_device").select_option("cuda")
    page.locator("#saveSettings").click()
    expect(page.locator("#notice")).to_contain_text("重启对应 Runtime")
    expect(page.locator("#notice")).to_contain_text("无需重启整个 stack")
