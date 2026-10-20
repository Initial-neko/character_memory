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
from test_space_reconciliation_playwright import space_server as _ensemble_live_server_fixture

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


class MissingMobileDrawerHandoff(AssertionError):
    """The AI drawer is open but still covered by its mobile entry sidebar."""


@pytest.fixture
def ensemble_live_server(tmp_path_factory):
    """Each control owns a fresh database, including its resumable READY build."""
    fixture = _ensemble_live_server_fixture.__wrapped__(tmp_path_factory)
    try:
        yield next(fixture)
    finally:
        fixture.close()


@pytest.mark.parametrize("width,height,entry", [
    pytest.param(390, 844, "ai", marks=pytest.mark.xfail(
        strict=True, raises=MissingMobileDrawerHandoff,
        reason="Known mobile AI-builder sidebar handoff defect; remove after repair")),
    (390, 844, "warm"),
    (1440, 900, "ai"),
    (1440, 900, "warm"),
])
def test_ensemble_sidebar_drawer_handoff(page, ensemble_live_server, width, height, entry):
    """Real HTTP/renderer: the destination drawer must receive pointer input."""
    from datetime import datetime, timezone
    import hashlib
    import subprocess

    space_server = ensemble_live_server
    page.set_viewport_size({"width": width, "height": height})
    page.set_default_timeout(10000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(space_server, wait_until="domcontentloaded")
    if width <= 820:
        page.locator("#sidebarMobileButton").click()
        assert page.locator("body").evaluate("el=>el.classList.contains('sidebar-mobile-open')")
    page.locator(".ensemble-create-button" if entry == "ai" else ".scene-entry-button").click()
    target = page.locator('[data-ensemble-template="warm-isekai"]' if entry == "ai" else '[data-ensemble-confirm]')
    expect(target).to_be_visible()
    expect(page.locator("#drawer")).to_have_attribute("aria-hidden", "false")
    page.locator("img").evaluate_all("imgs=>Promise.all(imgs.map(i=>i.decode().catch(()=>{})))")
    # Reach the actual target using normal browser scrolling, never force-click.
    target.scroll_into_view_if_needed()
    observation = target.evaluate("""el=>{
      const r=el.getBoundingClientRect();
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      return {sidebar_mobile_open:document.body.classList.contains('sidebar-mobile-open'),
        center_reaches_target:!!hit&&(hit===el||el.contains(hit)),
        center_hit:hit?.tagName+'.'+hit?.className,
        sidebar_z:getComputedStyle(document.querySelector('.sidebar')).zIndex,
        drawer_z:getComputedStyle(document.querySelector('#drawer')).zIndex};
    }""")
    click_error = None
    try:
        target.click(trial=True, timeout=1200)
    except Exception as exc:
        click_error = str(exc)
    evidence = Path(os.environ.get("CORE_VISUAL_EVIDENCE_DIR", "artifacts/core-visual-evidence"))
    evidence = evidence / "ensemble-handoff"
    evidence.mkdir(parents=True, exist_ok=True)
    stem = f"{entry}-{width}x{height}"
    image_path = evidence / f"{stem}.png"
    page.screenshot(path=str(image_path), animations="disabled")
    served = {}
    for name in ("index.html", "ensemble.js", "ensemble.css", "app.js", "sidebar_collapse.js", "chat_refine.css", "styles.css"):
        response = page.request.get(space_server + ("/" if name == "index.html" else "/static/" + name))
        assert response.ok
        payload = response.body().replace(b"\r\n", b"\n")
        committed = subprocess.check_output(["git", "show", f"HEAD:src/character_memory/web/{name}"], cwd=ROOT).replace(b"\r\n", b"\n")
        assert payload == committed, name
        served[name] = hashlib.sha256(payload).hexdigest()
    assert errors == [], errors
    # A passing control confirms the normal existing warm-experience handoff.
    payload = {"at": datetime.now(timezone.utc).isoformat(), "entry": entry,
        "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "runtime_identity": "Fresh existing isolated e2e process; served assets compared to exact committed content",
        "formal_runtime_sha": None, "browser": page.context.browser.version,
        "viewport": {"width": width, "height": height}, "observation": observation,
        "trial_click_error": click_error, "errors": errors, "served_sha256": served,
        "screenshot": {"path": image_path.name, "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest()},
        "boundary": "Temporary DB/media/personas; deterministic fixture; no confirm/creation, generated media, provider or formal data validation",
        "approved_reference": None, "design_consistency": "BLOCKED: no approved Ensemble/mobile reference", "human_signoff": "PENDING"}
    (evidence / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if width <= 820 and entry == "ai":
        assert observation["sidebar_mobile_open"]
        assert not observation["center_reaches_target"]
        assert "sidebar" in observation["center_hit"]
        assert click_error and "intercepts pointer events" in click_error
        raise MissingMobileDrawerHandoff("AI builder remains covered by the mobile sidebar")
    assert not observation["sidebar_mobile_open"]
    assert observation["center_reaches_target"]
    assert click_error is None

