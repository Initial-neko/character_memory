"""Real browser acceptance for relationship practice.

One isolated uvicorn process (temp SQLite + temp persona root) backs a real
Chromium page. The deterministic model still distinguishes the three partners,
so the screenshot and the DOM both show disagreement rather than a single
voice. Screenshots are written to SCENARIO_BROWSER_EVIDENCE_DIR when set.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1",
                                 reason="explicit browser runtime")]
ROOT = Path(__file__).resolve().parents[1]


def _open_scenario(page, base):
    """Open the real page without depending on a Direct SSE connection.

    The practice surface is independent of the direct-chat event stream, and
    this isolated fixture only needs the static app plus the practice routes.
    """
    from playwright.sync_api import expect

    page.goto(base, wait_until="domcontentloaded")
    expect(page.locator("#characterList .character-item").first).to_be_visible()
    expect(page.locator("#messageInput")).to_be_visible()


def _write_personas(root: Path) -> None:
    for name in ("rin", "momo"):
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "persona.yaml").write_text(
            f"id: {name}\nname: {name.title()}\nidentity: e2e scenario character\n",
            encoding="utf-8",
        )


@pytest.fixture
def scenario_browser_server(tmp_path):
    _write_personas(tmp_path / "personas")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "CHARACTER_MEMORY_SCENARIO_DB": str(tmp_path / "scenario.db"),
        "CHARACTER_MEMORY_SCENARIO_MEDIA": str(tmp_path / "media"),
        "CHARACTER_MEMORY_SCENARIO_PERSONAS": str(tmp_path / "personas"),
        "SCENARIO_MODEL_CALL_LOG": str(tmp_path / "model-calls.jsonl"),
    }
    options = {}
    tree = None
    if os.name == "nt":
        from character_memory.runtime.process_tree import WindowsProcessTree

        tree = WindowsProcessTree()
        options["creationflags"] = 0x00000004 | 0x08000000
    log = tmp_path / "scenario-browser.log"
    with log.open("wb") as stream:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "e2e_scenario_app:app",
             "--app-dir", str(ROOT / "tests"), "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=stream, stderr=stream, **options,
        )
        try:
            if tree:
                tree.attach_and_resume(proc)
            origin = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(log.read_text(encoding="utf-8", errors="replace"))
                try:
                    with urlopen(origin + "/health", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("scenario browser readiness timeout")
            yield origin
        finally:
            if tree:
                tree.close()
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)


def shot(page, tmp_path, name, base):
    root = Path(os.getenv("SCENARIO_BROWSER_EVIDENCE_DIR", str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.png"
    page.screenshot(path=str(path), animations="disabled")
    runtime = page.request.get(base + "/__e2e/scenario-runtime").json()
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).splitlines()
    meta = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(status),
        "git_status_porcelain": status,
        "api_pid": runtime["pid"],
        "model_calls": runtime["counts"],
        "viewport": page.viewport_size,
        "url": page.url,
        "screenshot_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    (root / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return path


def test_create_practice_read_distinct_views_and_reuse_drafts(page, scenario_browser_server, tmp_path):
    from playwright.sync_api import expect

    base = scenario_browser_server
    page.set_viewport_size({"width": 1440, "height": 1100})
    errors = []
    page.on("console", lambda message: errors.append(f"{message.type}: {message.text}")
            if message.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    _open_scenario(page, base)

    page.locator(".scenario-entry").click()
    expect(page.get_by_role("heading", name="创建一个情境")).to_be_visible()
    shot(page, tmp_path, "desktop-create-empty", base)
    page.locator('[data-scenario-field="title"]').fill("和家里说想换工作")
    page.locator('[data-scenario-field="situation"]').fill(
        "我正在考虑辞掉现在的工作，换一份收入不稳定但更喜欢的工作，今晚要和妈妈、姐姐吃饭。")
    page.locator('[data-scenario-field="goal"]').fill(
        "把计划说清楚，也听听她们的担心，不吵起来。")
    page.locator('[data-scenario-field="concern"]').fill(
        "怕一开口就变成辩解，也怕她们觉得我不负责任。")
    # Rin is preselected as the formal character.
    page.locator('[data-scenario-sim-name="0"]').fill("妈妈")
    page.locator('[data-scenario-sim-description="0"]').fill(
        "担心我辞职后没有稳定收入，习惯先问最坏的情况")
    page.locator('[data-scenario-sim-name="1"]').fill("姐姐")
    page.locator('[data-scenario-sim-description="1"]').fill(
        "务实，愿意帮我想步骤，但不喜欢听空话")
    with page.expect_response(lambda r: r.url.endswith("/v2/scenarios") and r.request.method == "POST") as created:
        page.get_by_role("button", name="创建并开始").click()
    assert created.value.status == 201, created.value.text()

    expect(page.locator(".scenario-brief h2")).to_have_text("和家里说想换工作")
    expect(page.locator("#characterIdentity")).to_contain_text("模拟练习")
    expect(page.locator("#characterIdentity")).to_contain_text("不写入现实记忆")
    expect(page.locator("#messageInput")).to_be_enabled()
    expect(page.get_by_text("模拟情境", exact=True)).to_be_visible()
    assert page.get_by_role("button", name="继续练习").count() == 0

    # --- one complete practice turn -----------------------------------
    sent = []
    page.on("request", lambda request: sent.append(request.post_data_json)
            if request.url.endswith("/messages") and "/v2/scenarios/" in request.url else None)
    page.locator("#messageInput").fill("我今晚想先讲我的计划，希望你们先听我说完。")
    with page.expect_response(lambda r: r.url.endswith("/messages") and "/v2/scenarios/" in r.url) as first:
        page.locator("#sendButton").click()
    assert first.value.status == 200, first.value.text()
    expect(page.locator(".scenario-view")).to_have_count(3)
    expect(page.locator(".scenario-reply")).to_have_count(2)
    stances = page.locator(".scenario-stance").all_text_contents()
    assert len(set(stances)) == 3, stances
    assert {"先照顾自己", "担心风险", "先算成本"} <= set(stances)
    assert page.get_by_text("妈妈 · 模拟回应").count() == 1
    assert page.get_by_text("姐姐 · 模拟回应").count() == 1
    assert sent and "reply_suggestion" not in sent[0]

    # Mobile data state before drafts: the composer stays one row, so the
    # briefing and at least the beginning of the distinct views are visible.
    page.set_viewport_size({"width": 400, "height": 1400})
    page.locator("#messageInput").blur()
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    page.evaluate("window.scrollTo(0, 0)")
    shot(page, tmp_path, "mobile-views-and-drafts", base)

    # --- drafts are the ordinary suggestion batch, not a second input --
    page.set_viewport_size({"width": 1440, "height": 1100})
    with page.expect_response(lambda r: r.url.endswith("/v2/reply-suggestions")
                              and r.request.method == "POST") as generated:
        page.get_by_role("button", name="帮我想想", exact=True).click()
    assert generated.value.status == 200, generated.value.text()
    batch = generated.value.json()
    expect(page.locator("[data-suggestion-id]")).to_have_count(3)
    page.evaluate("window.scrollTo(0, 0)")
    shot(page, tmp_path, "desktop-views-and-drafts", base)

    choice = batch["suggestions"][0]
    page.locator(f'[data-suggestion-id="{choice["id"]}"]').click()
    expect(page.locator("#messageInput")).to_have_value(choice["text"])
    with page.expect_response(lambda r: r.url.endswith("/messages") and "/v2/scenarios/" in r.url) as selected:
        page.locator("#sendButton").click()
    assert selected.value.status == 200, selected.value.text()
    expect(page.locator(".scenario-view")).to_have_count(6)
    assert sent[-1]["reply_suggestion"]["batch_id"] == batch["batch_id"]
    signals = page.request.get(base + f"/v2/reply-suggestions/{batch['batch_id']}/signals").json()["signals"]
    assert [item["kind"] for item in signals] == ["SELECT"], signals
    assert signals[0]["source_ref"]["kind"] == "SCENARIO_EVENT"

    # Free input uses the same composer and the same accepted-turn endpoint.
    page.locator("#messageInput").fill("我自己改一句：我们先吃饭，吃完再一起算账。")
    with page.expect_response(lambda r: r.url.endswith("/messages") and "/v2/scenarios/" in r.url) as free:
        page.locator("#sendButton").click()
    assert free.value.status == 200, free.value.text()
    expect(page.locator(".scenario-view")).to_have_count(9)
    assert "reply_suggestion" not in sent[-1]

    # --- a provider failure is explicit and fabricates nobody ----------
    perspectives_before = page.locator(".scenario-view").count()
    page.set_viewport_size({"width": 400, "height": 900})
    page.locator("#messageInput").fill("FAIL_SCENARIO 这一句会失败")
    with page.expect_response(lambda r: r.url.endswith("/messages") and "/v2/scenarios/" in r.url) as failed:
        page.locator("#sendButton").click()
    assert failed.value.status == 502, failed.value.text()
    expect(page.get_by_text("这一轮没有完成")).to_be_visible()
    expect(page.get_by_text("没有替任何人补写观点")).to_be_visible()
    assert page.locator(".scenario-view").count() == perspectives_before
    assert page.locator("#messageInput").input_value() == ""
    shot(page, tmp_path, "mobile-explicit-failure", base)

    # --- practice never becomes real memory ----------------------------
    audit = page.request.get(base + "/__e2e/audit").json()
    assert audit["scenario_events"] > 0
    assert audit["scenario_events_bad_scope"] == 0
    assert audit["memories"] == 0
    assert audit["real_leaks"] == {"memories": 0, "events": 0, "conversation_events": 0}
    page.locator('.scenario-brief [data-scenario-exit]').click()
    expect(page.locator(".scenario-brief")).to_have_count(0)
    expect(page.locator("#messageInput")).to_be_enabled()
    expect(page.locator("body")).not_to_have_class("scenario-mode")
    assert not [item for item in errors if item.startswith("pageerror")], errors
