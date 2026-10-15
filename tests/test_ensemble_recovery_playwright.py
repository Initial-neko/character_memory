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
  <section id="sidebarGroups"><div class="group-section-head"><div class="group-section-actions"></div></div></section>
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
    on: () => {},
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


def test_warm_template_previews_before_confirmation_and_creates_once(ensemble_page):
    page = ensemble_page
    page.evaluate("""() => {
      const draft = window.__resumableBuild.drafts[0].draft;
      window.__templateBuild = {
        ...window.__resumableBuild, group_name:'灯溪镇 · 栖灯旅店',
        mode:'ORIGINAL', drafts:['莉娅','芙露','赛琳','米娅'].map((name,index) => ({
          index, status:'READY', generation_method:'TEMPLATE', canonical_name:name,
          existing_character_id:null, draft:{...draft,name}
        }))
      };
      window.__calls = [];
      window.__enteredGroup = null;
      window.CM.features.groups = {loadGroups:async()=>{},enter:async id=>{window.__enteredGroup=id;}};
      window.CM.api = async (path, options={}) => {
        window.__calls.push({path,method:options.method || 'GET',body:options.body});
        if(path === '/v1/ensembles') return {build:null};
        if(path.endsWith('/confirm')) return {build:{...window.__templateBuild,group_id:'warm-active',status:'ACTIVE'}};
        return {build:window.__templateBuild};
      };
    }""")
    page.locator(".ensemble-create-button").click()
    expect(page.locator("[data-ensemble-template='warm-isekai']")).to_be_visible()
    page.locator("[data-ensemble-template='warm-isekai']").click()
    expect(page.locator(".ensemble-member-card")).to_have_count(4)
    expect(page.locator("[data-ensemble-regenerate]")).to_have_count(0)
    before = page.evaluate("window.__calls")
    assert [call["path"] for call in before] == [
        "/v1/ensembles", "/v1/ensembles/templates/warm-isekai/prepare"
    ]
    assert page.evaluate("window.__enteredGroup") is None
    page.locator("[data-ensemble-confirm]").click()
    page.wait_for_function("window.__enteredGroup === 'warm-active'")
    calls = page.evaluate("window.__calls")
    confirms = [call for call in calls if call["path"].endswith("/confirm")]
    assert len(confirms) == 1
    assert json.loads(confirms[0]["body"])["selected_indices"] == [0, 1, 2, 3]


def test_custom_scene_sends_four_fields_and_restores_failed_input(ensemble_page):
    page = ensemble_page
    page.evaluate("""() => {
      window.__baseBuild = window.__resumableBuild;
      window.__sceneCalls = [];
      window.CM.api = async (path, options={}) => {
        window.__sceneCalls.push({path,body:options.body});
        if(path === '/v1/ensembles') return {build:null};
        return {build:{...window.__baseBuild,mode:'SCENE',status:'FAILED',
          scene:JSON.parse(options.body),prompt:options.body}};
      };
    }""")
    page.locator(".ensemble-create-button").click()
    expect(page.locator("[data-ensemble-mode] option[value='SCENE']")).to_have_count(1)
    page.locator("[data-ensemble-mode]").select_option("SCENE")
    data = {
        "background": "温暖异世界的雨后小镇",
        "characters": "三个成年朋友，有不同关注",
        "relationships": "彼此熟悉，对我有好感",
        "opening": "我走进旅店，可以自由聊天",
    }
    for name, value in data.items():
        page.locator(f"[data-ensemble-scene-{name}]").fill(value)
    page.locator("[data-ensemble-start]").click()
    expect(page.locator("[data-ensemble-edit]")).to_be_visible()
    calls = page.evaluate("window.__sceneCalls")
    assert calls[-1]["path"] == "/v1/ensembles/scenes/prepare"
    assert json.loads(calls[-1]["body"]) == data
    page.locator("[data-ensemble-edit]").click()
    expect(page.locator("[data-ensemble-mode]")).to_have_value("SCENE")
    for name, value in data.items():
        expect(page.locator(f"[data-ensemble-scene-{name}]")).to_have_value(value)
