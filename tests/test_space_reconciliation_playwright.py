"""Browser acceptance for delayed Space comment-thread reconciliation."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
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
    pytest.mark.skipif(
        os.getenv("RUN_PLAYWRIGHT") != "1",
        reason="browser smoke runs in dedicated CI job",
    ),
]

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def space_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("space-ui")
    persona = root / "personas" / "rin"
    persona.mkdir(parents=True)
    shutil.copyfile(ROOT / "personas" / "rin" / "persona.yaml", persona / "persona.yaml")
    deferred_persona = root / "personas" / "deferred-space-role"
    deferred_persona.mkdir(parents=True)
    persona_text = (ROOT / "personas" / "rin" / "persona.yaml").read_text(encoding="utf-8")
    persona_text = persona_text.replace("id: rin\n", "id: deferred-space-role\n", 1)
    persona_text = persona_text.replace("name: Rin\n", "name: Deferred Space Role\n", 1)
    (deferred_persona / "persona.yaml").write_text(persona_text, encoding="utf-8")
    (deferred_persona / "direct_pending.yaml").write_text(
        "pending_at: '2026-10-02T09:00:00+08:00'\n", encoding="utf-8"
    )
    port = _free_port()
    env = os.environ.copy()
    env["CHARACTER_MEMORY_E2E_DB"] = str(root / "e2e.db")
    env["CHARACTER_MEMORY_E2E_MEDIA"] = str(root / "media")
    env["CHARACTER_MEMORY_E2E_PERSONA_ROOT"] = str(root / "personas")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "e2e_space_app:app",
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
    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"Space UI server exited early:\n{output}")
        try:
            with urlopen(f"{base}/health", timeout=0.5) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        process.terminate()
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError(f"Space UI server did not become ready:\n{output}")

    yield base

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def test_space_mention_picker_includes_deferred_roles(page, space_server):
    page.set_default_timeout(15000)
    default_characters = page.request.get(f"{space_server}/v1/characters").json()["characters"]
    all_characters = page.request.get(
        f"{space_server}/v1/characters?include_deferred=true"
    ).json()["characters"]
    assert "deferred-space-role" not in {item["id"] for item in default_characters}
    assert "deferred-space-role" in {item["id"] for item in all_characters}

    created = page.request.post(
        f"{space_server}/v1/space/posts",
        data={"character_id": "rin", "content": "deferred 角色 @ 选择器测试"},
    )
    assert created.ok
    post_id = str(created.json()["post"]["id"])

    page.goto(space_server, wait_until="domcontentloaded")
    page.locator(".space-nav-button").click()
    post = page.locator(f'[data-space-post="{post_id}"]')
    expect(post).to_be_visible()
    post.locator("[data-space-comments-panel-toggle]").click()
    post.locator("[data-space-mention-toggle]").click()
    expect(post.locator('[data-space-mention-id="deferred-space-role"]')).to_be_visible()


def test_open_comment_panel_re_reads_scheduler_replies(page, space_server):
    page.set_default_timeout(15000)
    created = page.request.post(
        f"{space_server}/v1/space/posts",
        data={"character_id": "rin", "content": "浏览器对账测试动态"},
    )
    assert created.ok
    post_id = str(created.json()["post"]["id"])

    page.goto(space_server, wait_until="domcontentloaded")
    page.locator(".space-nav-button").click()
    post = page.locator(f'[data-space-post="{post_id}"]')
    expect(post).to_be_visible()
    post.locator("[data-space-comments-panel-toggle]").click()
    post.locator(".space-comment-input").fill("先留下一句")
    post.locator(".space-comment-submit").click()
    expect(post.get_by_text("先留下一句", exact=True)).to_be_visible()

    current = page.request.get(f"{space_server}/v1/space/posts/{post_id}").json()["post"]
    user_comment = next(item for item in current["comments"] if item["content"] == "先留下一句")
    delayed = page.request.post(
        f"{space_server}/v1/space/posts/{post_id}/comments",
        data={
            "character_id": "rin",
            "content": "后台稍后写入的公开回复",
            "reply_to_comment_id": user_comment["id"],
        },
    )
    assert delayed.ok
    # A cold CI runner may finish the POST after the first scheduled refresh.
    # Only the eventual visibility is a stable product contract.
    expect(post.get_by_text("后台稍后写入的公开回复", exact=True)).to_be_visible(timeout=6000)


def test_delayed_reconcile_preserves_comment_draft_and_focus(page, space_server):
    """Background replies must not erase text typed after a comment is sent."""

    page.set_default_timeout(15000)
    response = page.request.post(
        f"{space_server}/v1/space/posts",
        data={"character_id": "rin", "content": "第二条草稿保护测试"},
    )
    assert response.ok
    post_id = str(response.json()["post"]["id"])
    page.goto(space_server, wait_until="domcontentloaded")
    page.locator(".space-nav-button").click()
    post = page.locator(f'[data-space-post="{post_id}"]')
    expect(post).to_be_visible()
    post.locator("[data-space-comments-panel-toggle]").click()
    post.locator(".space-comment-input").fill("第一条评论")
    post.locator(".space-comment-submit").click()
    expect(post.get_by_text("第一条评论", exact=True)).to_be_visible()

    input_box = post.locator(".space-comment-input")
    input_box.fill("准备发送的第二条评论，不应被刷新清空")
    input_box.focus()
    # Exercise the actual first 2.5s delayed background reconciliation.
    page.wait_for_timeout(3100)
    assert input_box.input_value() == "准备发送的第二条评论，不应被刷新清空"
    assert input_box.evaluate("(node) => document.activeElement === node")
