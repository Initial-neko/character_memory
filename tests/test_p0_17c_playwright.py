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
def wake_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("p0-17c-browser")
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


def test_manual_wake_uses_time_tick_and_existing_direct_sse(page, wake_server):
    page.set_default_timeout(10000)
    page.goto(wake_server, wait_until="domcontentloaded")
    expect(page.locator("#characterList .character-item").first).to_be_visible()
    expect(page.locator("#wakeButton")).to_be_visible()
    page.wait_for_function("() => Boolean(CM.state.directStream && CM.state.directStream.readyState === EventSource.OPEN)")

    page.locator("#wakeButton").click()
    expect(page.locator(".message-row.assistant .bubble", has_text="E2E wake reply")).to_be_visible()
    expect(page.locator("#wakeButton")).to_be_enabled()
