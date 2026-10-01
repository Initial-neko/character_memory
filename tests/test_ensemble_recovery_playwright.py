"""Real-browser contract for restoring a durable Ensemble confirmation.

The repository test pins restart recovery and the route payload. This test runs
the shipped IIFE against a real DOM, so a drawer that silently forgets to ask
for the durable build cannot pass by merely retaining an endpoint string.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

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
WEB = ROOT / "src" / "character_memory" / "web"

_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>ensemble recovery</title></head>
<body>
  <section id="sidebarGroups"><div class="group-section-actions"></div></section>
  <div id="drawerBody"></div>
  <script>
  window.CM = {
    state: {characters: [], characterCapacity: {activeTotal:0, softLimit:10, hardLimit:20}},
    features: {groups: {}},
    dom: {drawerBody: document.getElementById("drawerBody")},
    api: async path => {
      window.__ensembleRequests = [...(window.__ensembleRequests || []), path];
      return {build: window.__resumableBuild};
    },
    openDrawer: () => {},
    closeDrawer: () => {},
    loadCharacters: async () => {},
    registerFeature: (name, feature) => { window.CM.features[name] = feature; },
    initialFor: draft => String(draft.name || "?").slice(0, 1),
    escapeHtml: value => String(value).replace(/[&<>"']/g, ch => (
      {"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[ch]
    )),
  };
  </script>
</body></html>"""


@pytest.fixture
def ensemble_page(page):
    page.set_default_timeout(15000)
    page.set_content(_PAGE, wait_until="domcontentloaded")
    page.evaluate(
        "build => { window.__resumableBuild = build; }",
        {
            "group_id": "ensemble-resume",
            "status": "READY",
            "group_name": "未来道具研究所",
            "overview": "保存后的成员草稿",
            "drafts": [
                {
                    "index": 0,
                    "status": "READY",
                    "canonical_name": "Kurisu",
                    "existing_character_id": None,
                    "draft": {"name": "Kurisu", "identity": "研究者"},
                },
                {
                    "index": 1,
                    "status": "READY",
                    "canonical_name": "Okabe",
                    "existing_character_id": None,
                    "draft": {"name": "Okabe", "identity": "实验室负责人"},
                },
            ],
            "capacity": {"active_count": 0, "soft_limit": 10, "hard_limit": 20},
            "sources": [],
        },
    )
    page.add_script_tag(path=str(WEB / "ensemble.js"))
    return page


def test_opening_builder_restores_ready_confirmation(ensemble_page):
    ensemble_page.locator(".ensemble-create-button").click()

    expect(ensemble_page.locator(".ensemble-confirmation")).to_be_visible()
    expect(ensemble_page.locator("h3")).to_have_text("未来道具研究所")
    expect(ensemble_page.locator(".ensemble-member-card")).to_have_count(2)
    requests = ensemble_page.evaluate("window.__ensembleRequests")
    assert requests == ["/v1/ensembles"], json.dumps(requests, ensure_ascii=False)
