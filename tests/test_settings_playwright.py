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


def test_tts_provider_switch_updates_voice_device_and_persists_without_full_stack_restart(page, settings_server):
    page.set_default_timeout(10000)
    page.goto(f"{settings_server}/settings", wait_until="domcontentloaded")

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
    page.locator("#reloadSettings").click()
    expect(provider).to_have_value("gsv")
    expect(voice).to_have_value("murasame")
    expect(device).to_have_value("cuda")


def test_non_hot_local_device_change_is_explicitly_restart_required(page, settings_server):
    page.set_default_timeout(10000)
    page.goto(f"{settings_server}/settings", wait_until="domcontentloaded")

    provider = page.locator("#setting-tts_provider")
    provider.select_option("kokoro")
    page.locator("#saveSettings").click()
    expect(page.locator("#notice")).to_contain_text("无需重启整个 stack")

    page.locator("#setting-tts_device").select_option("cuda")
    page.locator("#saveSettings").click()
    expect(page.locator("#notice")).to_contain_text("重启对应 Runtime")
    expect(page.locator("#notice")).not_to_contain_text("重启整个 stack")
