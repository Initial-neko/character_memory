"""Front-end acceptance for the shared layout density.

These assert the things a screenshot review caught: the sidebar must not clip
rows in a nested scroll box, the fixed chrome must stay small, and the content
column must get the reclaimed width. Run with RUN_PLAYWRIGHT=1.
"""
import os
import shutil
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="explicit browser run"),
]


@pytest.fixture(scope="module")
def density_server(tmp_path_factory):
    import uvicorn

    from character_memory.api import create_api
    from character_memory.group_store import GroupRepository
    from character_memory.group_web import attach_group_routes
    from character_memory.history_web import attach_history_routes

    root = Path(__file__).parents[1]
    tmp = tmp_path_factory.mktemp("ui-density")
    for name in ("rin", "momo"):
        folder = tmp / "personas" / name
        folder.mkdir(parents=True)
        shutil.copyfile(root / "personas" / name / "persona.yaml", folder / "persona.yaml")
    config = tmp / "config.yaml"
    config.write_text(
        f'api_key: ""\nembedding_provider: deterministic\ndb_path: "{(tmp / "density.db").as_posix()}"\n'
        f'persona_path: "{(tmp / "personas/rin/persona.yaml").as_posix()}"\n',
        encoding="utf-8",
    )
    app = create_api(str(config))
    attach_history_routes(app)
    attach_group_routes(app, str(config))
    access = app.state.character_memory
    ids = [profile["id"] for profile in access.character_profiles()]
    now = datetime.now(timezone.utc).astimezone()
    repository = GroupRepository(access.store())
    for index in range(1, 6):
        repository.create_group(f"验收群聊 {index}", ids, now)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, name="ui-density-server")
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def _ready(page, base):
    page.goto(base + "/")
    page.wait_for_function("window.CM && document.body.dataset.cmReady === 'true'", timeout=20000)
    page.wait_for_function("document.querySelectorAll('.group-item').length >= 1", timeout=20000)


def _sidebar_metrics(page):
    return page.evaluate("""() => {
      const body = document.querySelector('.sidebar-body');
      const groupList = document.querySelector('.group-list');
      const rows = [...document.querySelectorAll('.character-item, .group-item')];
      const clipped = rows.filter(row => row.scrollWidth > row.clientWidth + 1).length;
      const bodyRect = body.getBoundingClientRect();
      const outside = rows.filter(row => {
        const rect = row.getBoundingClientRect();
        return rect.left < bodyRect.left - 1 || rect.right > bodyRect.right + 1;
      }).length;
      return {
        bodyOverflowY: getComputedStyle(body).overflowY,
        groupOverflowY: getComputedStyle(groupList).overflowY,
        groupMaxHeight: getComputedStyle(groupList).maxHeight,
        clipped,
        outside,
        sidebarWidth: document.querySelector('.sidebar').getBoundingClientRect().width,
        headerHeight: document.querySelector('.cm-global-header').getBoundingClientRect().height,
        chatWidth: document.querySelector('.chat').getBoundingClientRect().width,
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      };
    }""")


def test_desktop_sidebar_has_one_scroll_owner_and_the_content_column_grew(page, density_server):
    page.set_viewport_size({"width": 1440, "height": 900})
    _ready(page, density_server)
    metrics = _sidebar_metrics(page)

    assert metrics["bodyOverflowY"] == "auto"
    assert metrics["groupOverflowY"] == "visible"
    assert metrics["groupMaxHeight"] == "none"
    assert metrics["clipped"] == 0
    assert metrics["outside"] == 0
    assert 220 <= metrics["sidebarWidth"] <= 245
    assert metrics["headerHeight"] <= 58
    assert metrics["chatWidth"] >= 1100
    assert metrics["scrollWidth"] <= metrics["clientWidth"]

    folder = os.getenv("UI_EVIDENCE_DIR")
    if folder:
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        page.locator(".sidebar").screenshot(path=str(target / "density-desktop-sidebar.png"))


def test_mobile_drawer_keeps_every_group_row_reachable(page, density_server):
    page.set_viewport_size({"width": 400, "height": 844})
    _ready(page, density_server)
    page.evaluate("document.body.classList.add('sidebar-mobile-open')")
    page.wait_for_timeout(300)

    metrics = _sidebar_metrics(page)
    assert metrics["clipped"] == 0
    assert metrics["outside"] == 0
    assert metrics["scrollWidth"] <= metrics["clientWidth"]

    page.evaluate("document.querySelector('.sidebar-body').scrollTop = 1e6")
    page.wait_for_timeout(100)
    last_row_visible = page.evaluate("""() => {
      const body = document.querySelector('.sidebar-body');
      const rows = [...document.querySelectorAll('.group-item')];
      const row = rows[rows.length - 1].getBoundingClientRect();
      const box = body.getBoundingClientRect();
      return row.top >= box.top - 1 && row.bottom <= box.bottom + 1;
    }""")
    assert last_row_visible

    folder = os.getenv("UI_EVIDENCE_DIR")
    if folder:
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        page.locator(".sidebar").screenshot(path=str(target / "density-mobile-sidebar.png"))


def test_life_rail_starts_below_the_shared_header(page, density_server):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(density_server + "/life")
    page.wait_for_function("document.querySelector('.life-nav')", timeout=20000)
    metrics = page.evaluate("""() => ({
      header: document.querySelector('.cm-global-header').getBoundingClientRect().height,
      railTop: document.querySelector('.life-nav').getBoundingClientRect().top,
    })""")
    assert metrics["railTop"] == metrics["header"]
    assert metrics["header"] <= 58
