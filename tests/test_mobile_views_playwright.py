"""Mobile browser acceptance with real-rendered screenshots for Core UI flows.

These checks run only in the dedicated Playwright job. Dev usage responses and
image generation are intercepted with deterministic fixtures; Space and chat
use the existing fake runtime backed by a temporary SQLite database.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
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
MOBILE_VIEWPORT = {"width": 430, "height": 932}
DEFERRED_ROLE_ID = "deferred-space-role"
FAKE_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg=="
)


def _visual_run_dir() -> Path:
    configured = os.getenv("CORE_VISUAL_EVIDENCE_DIR")
    base = Path(configured) if configured else ROOT / "artifacts" / "core-visual-evidence"
    if not base.is_absolute():
        base = ROOT / base
    run_id = os.getenv("GITHUB_RUN_ID") or f"local-{os.getpid()}"
    attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1")
    return base / f"{run_id}-{attempt}"


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _record_visual(page, scene: str, source_paths: tuple[str, ...]) -> None:
    """Save an actual browser capture and append its source hashes to a manifest."""

    run_dir = _visual_run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = run_dir / f"{scene}.png"
    page.screenshot(path=str(screenshot_path), full_page=False, animations="disabled")

    source_hashes = {
        source_path: hashlib.sha256((ROOT / source_path).read_bytes()).hexdigest()
        for source_path in source_paths
    }
    source_sha = hashlib.sha256(
        json.dumps(source_hashes, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    manifest_path = run_dir / "manifest.json"
    manifest = {
        "schema_version": 1,
        "git_commit": _git_commit(),
        "scenarios": [],
    }
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    relative_path = screenshot_path.relative_to(ROOT).as_posix()
    manifest["scenarios"].append(
        {
            "scene": scene,
            "path": relative_path,
            "sha256": hashlib.sha256(screenshot_path.read_bytes()).hexdigest(),
            "source_sha": source_sha,
            "source_files_sha256": source_hashes,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "viewport": page.evaluate("() => ({width: innerWidth, height: innerHeight})"),
        }
    )
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_manifest.replace(manifest_path)


def _usage_summary_payload() -> dict:
    feature = {
        "feature": "DIRECT",
        "purpose": "DIRECT_REACTION",
        "requests": 2,
        "logical_calls": 1,
        "requests_per_logical_call": 2,
        "input_chars": 400,
        "input_tokens": 800,
        "output_tokens": 500,
        "total_tokens": 1300,
        "token_known_requests": 2,
        "retried_logical_calls": 1,
        "errors": 0,
        "avg_latency_ms": 123,
    }
    model = {
        "model": "fake-model",
        "requests": 2,
        "logical_calls": 1,
        "input_tokens": 800,
        "output_tokens": 500,
        "total_tokens": 1300,
        "token_known_requests": 2,
        "retried_logical_calls": 1,
        "errors": 0,
        "avg_latency_ms": 123,
    }
    recent = {
        "created_at": "2026-10-03T00:00:00+00:00",
        "feature": "DIRECT",
        "purpose": "DIRECT_REACTION",
        "character_id": "rin",
        "model": "fake-model",
        "attempt": 2,
        "total_tokens": 1300,
        "duration_ms": 123,
        "status": "SUCCEEDED",
    }
    return {
        "summary": {
            "requests": 2,
            "logical_calls": 1,
            "token_known_requests": 2,
            "total_tokens": 1300,
            "input_tokens": 800,
            "output_tokens": 500,
            "input_chars": 400,
            "retried_logical_calls": 1,
            "errors": 0,
            "avg_latency_ms": 123,
        },
        "by_feature": [feature],
        "by_model": [model],
        "recent": [recent],
    }


def _mock_usage_response(page, *, payload: dict | None = None, status: int = 200, detail: str = ""):
    urls: list[str] = []

    def fulfill(route):
        urls.append(route.request.url)
        response_payload = {"detail": detail} if status >= 400 else (payload or {})
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(response_payload, ensure_ascii=False),
        )

    page.route("**/v1/dev/llm-usage*", fulfill)
    return urls


def _assert_default_one_hour_request(urls: list[str]) -> None:
    matches = [
        parse_qs(urlparse(url).query)
        for url in urls
        if urlparse(url).path == "/v1/dev/llm-usage"
    ]
    assert matches, f"no LLM usage request captured: {urls}"
    assert any(query.get("hours") == ["1"] and query.get("limit") == ["80"] for query in matches), matches


@pytest.fixture(scope="module")
def dev_console_url(tmp_path_factory):
    uvicorn = pytest.importorskip("uvicorn")
    httpx = pytest.importorskip("httpx")
    from character_memory.config import Settings
    from character_memory.dev_server import create_dev_app

    root = tmp_path_factory.mktemp("mobile-dev-console")

    def no_external_upstream(request):
        return httpx.Response(
            503,
            json={"detail": "blocked by deterministic browser fixture"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(no_external_upstream))
    app = create_dev_app(
        settings=Settings(db_path=str(root / "usage.sqlite3"), embedding_provider="deterministic"),
        http_client=client,
        model_factory=lambda _settings: object(),
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        worker.join(timeout=6)
        client.close()
        raise RuntimeError("the Dev Console did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    worker.join(timeout=6)
    client.close()


def _open_dev_console(page, base_url: str) -> None:
    page.set_viewport_size(MOBILE_VIEWPORT)
    page.goto(f"{base_url}/dev", wait_until="domcontentloaded")
    expect(page.locator("body")).to_have_attribute("data-dev-booted", "1")
    expect(page.locator("#llmUsageCard")).to_be_visible()


def test_dev_usage_default_window_renders_real_fields_and_request_rows(page, dev_console_url):
    urls = _mock_usage_response(page, payload=_usage_summary_payload())
    _open_dev_console(page, dev_console_url)

    expect(page.locator("#llmUsageWindow")).to_have_value("1")
    expect(page.locator("#llmUsageWindowBadge")).to_have_text("1H")
    expect(page.locator("#usageRequests")).to_have_text("2")
    expect(page.locator("#usageLogicalCalls")).to_have_text("1 logical")
    expect(page.locator("#usageTotalTokens")).to_have_text("1,300")
    expect(page.locator("#usageInputTokens")).to_have_text("800")
    expect(page.locator("#usageOutputTokens")).to_have_text("500")
    expect(page.locator("#usageCoverage")).to_have_text("token coverage 100.0%")
    expect(page.locator("#usageRetryRate")).to_have_text("100.0%")
    expect(page.locator("#usageRetryCalls")).to_have_text("1 logical calls")
    expect(page.locator("#usageErrorRate")).to_have_text("0.0%")
    expect(page.locator("#usageLatency")).to_have_text("avg 123 ms")
    _assert_default_one_hour_request(urls)
    _record_visual(
        page,
        "dev-usage-mobile-default-1h",
        ("src/character_memory/web/dev.html", "src/character_memory/web/dev.js"),
    )

    page.locator("#devModeToggle").click()
    diagnostic = page.locator('#llmUsageCard details[data-level="diagnostic"]')
    diagnostic.locator("summary").click()
    expect(page.locator("#llmUsageFeatureBody")).to_contain_text("私聊")
    expect(page.locator("#llmUsageFeatureBody")).to_contain_text("私聊回复")
    expect(page.locator("#llmUsageModelBody")).to_contain_text("fake-model")
    expect(page.locator("#llmUsageRecentBody")).to_contain_text("SUCCEEDED")
    expect(page.locator("#llmUsageRecentBody")).to_contain_text("1,300")
    page.locator("#llmUsageFeatureBody").scroll_into_view_if_needed()
    _record_visual(
        page,
        "dev-usage-mobile-attribution-tables",
        ("src/character_memory/web/dev.html", "src/character_memory/web/dev.js"),
    )


def test_dev_usage_empty_state_renders_zero_summary_and_empty_tables(page, dev_console_url):
    urls = _mock_usage_response(page, payload={"summary": {}, "by_feature": [], "by_model": [], "recent": []})
    _open_dev_console(page, dev_console_url)

    expect(page.locator("#usageRequests")).to_have_text("0")
    expect(page.locator("#usageTotalTokens")).to_have_text("—")
    expect(page.locator("#usageCoverage")).to_have_text("token coverage 0%")
    expect(page.locator("#usageRetryRate")).to_have_text("0%")
    _assert_default_one_hour_request(urls)
    page.locator("#devModeToggle").click()
    diagnostic = page.locator('#llmUsageCard details[data-level="diagnostic"]')
    diagnostic.locator("summary").click()
    for table_id in ("llmUsageFeatureBody", "llmUsageModelBody", "llmUsageRecentBody"):
        expect(page.locator(f"#{table_id}")).to_contain_text("暂无数据")
    page.locator("#llmUsageFeatureBody").scroll_into_view_if_needed()
    _record_visual(
        page,
        "dev-usage-mobile-empty",
        ("src/character_memory/web/dev.html", "src/character_memory/web/dev.js"),
    )


def test_dev_usage_failure_state_explains_unavailable_response(page, dev_console_url):
    urls = _mock_usage_response(page, status=503, detail="fake usage unavailable")
    _open_dev_console(page, dev_console_url)

    expect(page.locator("#usageRequests")).to_have_text("0")
    page.locator("#devModeToggle").click()
    diagnostic = page.locator('#llmUsageCard details[data-level="diagnostic"]')
    diagnostic.locator("summary").click()
    expect(page.locator("#llmUsageFeatureBody")).to_contain_text(
        "Usage unavailable: fake usage unavailable"
    )
    _assert_default_one_hour_request(urls)
    page.locator("#llmUsageFeatureBody").scroll_into_view_if_needed()
    _record_visual(
        page,
        "dev-usage-mobile-failure",
        ("src/character_memory/web/dev.html", "src/character_memory/web/dev.js"),
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def space_server(tmp_path):
    root = tmp_path / "space-e2e"
    persona = root / "personas" / "rin"
    persona.mkdir(parents=True)
    shutil.copyfile(ROOT / "personas" / "rin" / "persona.yaml", persona / "persona.yaml")
    deferred_persona = root / "personas" / DEFERRED_ROLE_ID
    deferred_persona.mkdir(parents=True)
    persona_text = (ROOT / "personas" / "rin" / "persona.yaml").read_text(encoding="utf-8")
    persona_text = persona_text.replace("id: rin\n", f"id: {DEFERRED_ROLE_ID}\n", 1)
    persona_text = persona_text.replace("name: Rin\n", "name: Deferred Space Role\n", 1)
    (deferred_persona / "persona.yaml").write_text(persona_text, encoding="utf-8")
    (deferred_persona / "direct_pending.yaml").write_text(
        "pending_at: '2026-10-02T09:00:00+08:00'\n", encoding="utf-8"
    )

    port = _free_port()
    env = os.environ.copy()
    env["CHARACTER_MEMORY_E2E_DB"] = str(root / "e2e.sqlite3")
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
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"fake Core E2E server exited early:\n{output}")
        try:
            with urlopen(f"{base_url}/health", timeout=0.5) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        process.terminate()
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError(f"fake Core E2E server did not become ready:\n{output}")

    yield base_url

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _open_space_on_mobile(page, base_url: str) -> None:
    page.set_viewport_size(MOBILE_VIEWPORT)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator(".space-nav-button").wait_for(state="attached")
    page.locator("#sidebarMobileButton").click()
    page.locator(".space-nav-button").click()


def test_space_comment_ui_submits_deferred_role_as_structured_mention(page, space_server):
    created = page.request.post(
        f"{space_server}/v1/space/posts",
        data={"character_id": "rin", "content": "移动端结构化提及验收"},
    )
    assert created.ok
    post_id = str(created.json()["post"]["id"])

    _open_space_on_mobile(page, space_server)
    post = page.locator(f'[data-space-post="{post_id}"]')
    expect(post).to_be_visible()
    post.locator("[data-space-comments-panel-toggle]").click()
    post.locator("[data-space-mention-toggle]").click()
    option = post.locator(f'[data-space-mention-id="{DEFERRED_ROLE_ID}"]')
    expect(option).to_be_visible()
    expect(option).to_have_text("Deferred Space Role")
    option.click()
    expect(post.locator(".space-mention-chip")).to_contain_text("Deferred Space Role")
    post.locator(".space-comment-input").fill("请看这条结构化提醒")

    with page.expect_request(
        lambda request: request.method == "POST"
        and urlparse(request.url).path == f"/v1/space/posts/{post_id}/comments"
    ) as comment_request:
        post.locator(".space-comment-submit").click()
    payload = json.loads(comment_request.value.post_data or "{}")
    assert payload["content"] == "请看这条结构化提醒"
    assert payload["mentions"] == [DEFERRED_ROLE_ID]
    expect(post.get_by_text("请看这条结构化提醒", exact=True)).to_be_visible()
    expect(post.locator(".space-comment-mention")).to_contain_text("Deferred Space Role")
    post.locator(".space-comment-text").last.scroll_into_view_if_needed()
    _record_visual(
        page,
        "space-mobile-deferred-mention-submit",
        (
            "src/character_memory/web/space.js",
            "src/character_memory/web/space.css",
            "src/character_memory/space_web.py",
            "src/character_memory/space_store.py",
        ),
    )


def test_space_notification_opens_target_marks_read_and_highlights_comment(page, space_server):
    created = page.request.post(
        f"{space_server}/v1/space/posts",
        data={"character_id": "rin", "content": "移动端通知定位验收"},
    )
    assert created.ok
    post_id = str(created.json()["post"]["id"])
    user_comment = page.request.post(
        f"{space_server}/v1/space/posts/{post_id}/comments",
        data={"content": "这里需要角色回复", "client_request_id": "mobile-notice-user"},
    )
    assert user_comment.ok
    user_comment_id = user_comment.json()["comment"]["id"]
    character_reply = page.request.post(
        f"{space_server}/v1/space/posts/{post_id}/comments",
        data={
            "character_id": "rin",
            "content": "这是目标通知对应的回复",
            "reply_to_comment_id": user_comment_id,
            "client_request_id": "mobile-notice-reply",
        },
    )
    assert character_reply.ok
    reply_id = str(character_reply.json()["comment"]["id"])
    unread = page.request.get(f"{space_server}/v1/space/notifications?unread_only=true&limit=20")
    assert unread.ok
    target = next(
        item
        for item in unread.json()["notifications"]
        if str(item["comment_id"]) == reply_id
    )
    assert "REPLY" in target["reasons"]
    notification_id = str(target["id"])

    page.set_viewport_size(MOBILE_VIEWPORT)
    page.goto(space_server, wait_until="domcontentloaded")
    notice_button = page.locator(".space-notification-button")
    notice_button.click()
    notice_item = page.locator(f'[data-space-notification="{notification_id}"]')
    expect(notice_item).to_be_visible()
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and urlparse(response.url).path == f"/v1/space/notifications/{notification_id}/read"
    ) as read_response:
        notice_item.click()
    response = read_response.value
    assert response.ok
    assert response.json()["notification"]["read_at"]

    post = page.locator(f'[data-space-post="{post_id}"]')
    comment = post.locator(f'[data-space-comment="{reply_id}"]')
    expect(post).to_be_visible()
    expect(comment).to_be_visible()
    expect(comment).to_have_class(re.compile(r"\bspace-comment-notified\b"))
    remaining = page.request.get(
        f"{space_server}/v1/space/notifications?unread_only=true&limit=20"
    )
    assert notification_id not in {str(item["id"]) for item in remaining.json()["notifications"]}
    comment.scroll_into_view_if_needed()
    _record_visual(
        page,
        "space-mobile-notification-read-highlight",
        (
            "src/character_memory/web/space.js",
            "src/character_memory/web/space.css",
            "src/character_memory/space_web.py",
            "src/character_memory/space_store.py",
        ),
    )


def test_ai_image_generation_uses_one_request_then_waits_for_send_confirmation(page, space_server):
    page.set_viewport_size(MOBILE_VIEWPORT)
    page.goto(space_server, wait_until="domcontentloaded")
    page.locator(".composer-tools-trigger").click()
    page.locator(".ai-image-trigger").wait_for(state="visible")

    image_requests: list[tuple[str, dict]] = []

    def fake_image_endpoint(route):
        request = route.request
        request_path = urlparse(request.url).path
        payload = json.loads(request.post_data or "{}")
        image_requests.append((request_path, payload))
        if request_path.endswith("/images/generate"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ok": True,
                        "character_id": "rin",
                        "purpose": "SCENE",
                        "provider": "fake-image-provider",
                        "model": "fake-image-model",
                        "prompt": "fake polished image prompt",
                        "duration_ms": 1,
                        "image": {
                            "filename": "fake-scene.png",
                            "data_url": FAKE_PNG_DATA_URL,
                            "size_bytes": 68,
                            "source": "AI_GENERATED_DRAFT",
                        },
                    },
                    ensure_ascii=False,
                ),
            )
        else:
            route.fulfill(status=500, content_type="application/json", body='{"detail":"unexpected image route"}')

    page.route("**/v1/characters/*/images/**", fake_image_endpoint)
    chat_requests = []
    page.on(
        "request",
        lambda request: chat_requests.append(request)
        if request.method == "POST" and urlparse(request.url).path == "/v1/chat/messages"
        else None,
    )

    page.locator(".ai-image-trigger").click()
    instruction = "窗边阅读的日常场景"
    page.locator("[data-ai-image-instruction]").fill(instruction)
    page.locator("[data-ai-image-generate]").click()
    send_button = page.locator(".image-panel [data-image-send]")
    expect(send_button).to_be_visible()
    expect(send_button).to_have_text("发送图片")
    expect(page.locator(".image-panel #imageCaption")).to_be_visible()
    assert [path for path, _payload in image_requests] == [
        "/v1/characters/rin/images/generate"
    ], image_requests
    assert image_requests[0][1]["instruction"] == instruction
    assert not chat_requests, "generated images must wait for the explicit send confirmation"
    page.locator(".image-panel").scroll_into_view_if_needed()
    _record_visual(
        page,
        "chat-mobile-ai-image-send-confirmation",
        (
            "src/character_memory/web/ai_images.js",
            "src/character_memory/web/images.js",
            "src/character_memory/web/app.js",
        ),
    )

    page.locator(".image-panel #imageCaption").fill("用户确认后的图片说明")
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and urlparse(response.url).path == "/v1/chat/messages"
    ) as send_response:
        send_button.click()
    assert send_response.value.ok
    assert len(chat_requests) == 1
    sent_payload = json.loads(chat_requests[0].post_data or "{}")
    assert sent_payload["message"] == "用户确认后的图片说明"
    assert sent_payload["image"]["filename"] == "fake-scene.png"
    expect(page.locator(".image-panel [data-image-send]")).to_have_count(0)
