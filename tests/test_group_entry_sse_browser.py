"""Native group SSE ownership during overlapping room entries.

Known failure is narrowly marked; remove the marker when the implementation is
fixed. With the marker retained, a successful fixed run is a strict XPASS.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

# This also permits running the review draft from an ignored evidence directory.
ROOT = next(path for path in Path(__file__).resolve().parents if (path / "pyproject.toml").is_file())
sys.path.insert(0, str(ROOT / "tests"))
from test_life_conversation import isolated_app
from test_life_conversation_browser import live_life

pytestmark = [pytest.mark.browser, pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT") != "1", reason="explicit isolated browser validation")]


class StaleGroupSubscription(AssertionError):
    """The proven stale-entry ownership violation, not a fixture error."""


@pytest.mark.parametrize("width", [1440, 390])
def test_late_group_entry_preserves_current_room_subscription(page, live_life, tmp_path, width):
    from playwright.sync_api import expect
    from character_memory.application.async_conversation import group_channel
    from character_memory.group_store import GroupEvent, GroupRepository

    base, character, _, store, access = live_life
    repo = GroupRepository(store)
    members = [p["id"] for p in access.character_profiles()]
    group_a = repo.create_group("AUDIT A delayed entry", members, datetime.now(timezone.utc))
    group_b = repo.create_group("AUDIT B current room", members, datetime.now(timezone.utc))
    configured = os.getenv("GROUP_ENTRY_EVIDENCE_DIR")
    out = Path(configured) if configured else tmp_path / "evidence"
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": width, "height": 960 if width == 1440 else 844})
    # Proxy only records native objects and close calls; all transport, open,
    # message dispatch and production listeners still use the real EventSource.
    page.add_init_script("""(() => {
      const Native=window.EventSource;
      window.__groupEntrySources=[];
      window.EventSource=new Proxy(Native,{construct(Target,args){
        const source=new Target(...args), entry={url:String(args[0]),source,closedAt:null};
        const close=source.close.bind(source);
        source.close=()=>{entry.closedAt=performance.now();return close();};
        window.__groupEntrySources.push(entry);return source;
      }});
    })();""")
    page.goto(base + "/")
    page.wait_for_function("document.body.dataset.cmReady === 'true'")
    page.evaluate("""() => {
      window.__groupEntryChanges=[];
      CM.on('conversationChanged', payload => {
        window.__groupEntryChanges.push({payload,state:{...CM.state.conversation}});
      });
    }""")

    def append_and_publish(content):
        event = repo.append_event(GroupEvent(
            conversation_id=group_b.id, turn_id=content, actor_type="CHARACTER", actor_id=character,
            event_type="CHARACTER_MESSAGE", event_time=datetime.now(timezone.utc), content=content,
            metadata={"action": "MESSAGE"}))
        sequence = access.stream_hub.publish(group_channel(group_b.id), "group_character_event", event.model_dump(mode="json"))
        return event, sequence

    def snapshot():
        return page.evaluate("""() => ({
          conversation:{...CM.state.conversation},heading:CM.dom.characterName.textContent,
          chat:CM.dom.chat.innerText,changes:window.__groupEntryChanges,
          sources:window.__groupEntrySources.map(s=>({url:s.url,readyState:s.source.readyState,closedAt:s.closedAt}))
        })""")

    held = []
    page.route("**/v1/groups/" + group_a.id + "/history?*", lambda route: held.append(route))
    # The actual exported production entry is the same closure invoked by the
    # sidebar click handler. Keeping its Promise lets a fixed implementation
    # complete without waiting for the broken A stream to open.
    with page.expect_request(lambda r: f"/v1/groups/{group_a.id}/history?" in r.url):
        page.evaluate("""id => {
          window.__groupEntryFinished=false;
          window.__groupEntryPromise=CM.features.groups.enter(id).then(
            ()=>{window.__groupEntryFinished=true;},
            error=>{window.__groupEntryError=String(error);window.__groupEntryFinished=true;});
        }""", group_a.id)
    # The request event can precede Playwright's route callback. Pump protocol
    # events until the actual route handle is available, with a bounded failure.
    route_deadline = time.monotonic() + 5
    while not held and time.monotonic() < route_deadline:
        page.wait_for_timeout(10)
    assert held
    if width == 390:
        page.locator("#sidebarMobileButton").click()
    page.locator(f'[data-group="{group_b.id}"]').click()
    expect(page.locator("#characterName")).to_have_text(group_b.name)
    page.wait_for_function("id => window.__groupEntrySources.some(s => s.url.includes(id) && s.source.readyState === EventSource.OPEN)", arg=group_b.id)
    initial, _ = append_and_publish("AUDIT B live before stale entry")
    expect(page.locator(".chat")).to_contain_text(initial.content)
    before = snapshot()

    held.pop().continue_()
    page.wait_for_function("window.__groupEntryFinished === true")
    assert page.evaluate("window.__groupEntryError || null") is None
    page.wait_for_function("""() => {
      const groups=window.__groupEntrySources.filter(s=>s.url.includes('scope=group'));
      return groups.some(s=>s.source.readyState===EventSource.OPEN) &&
        groups.every(s=>s.source.readyState!==EventSource.CONNECTING);
    }""")
    after = snapshot()
    assert after["conversation"] == {"type": "GROUP", "groupId": group_b.id}
    assert after["heading"] == group_b.name
    open_sources = [s for s in after["sources"] if "scope=group" in s["url"] and s["readyState"] == 1]
    assert len(open_sources) == 1
    stale = group_a.id in open_sources[0]["url"]
    if stale:
        assert any(group_b.id in s["url"] and s["readyState"] == 2 for s in after["sources"])
        assert any(c["payload"].get("groupId") == group_a.id and c["state"]["groupId"] == group_b.id for c in after["changes"])
    else:
        assert group_b.id in open_sources[0]["url"]

    sent, sequence = append_and_publish("AUDIT B durable live message after stale entry")
    if stale:
        page.wait_for_timeout(500)
        assert sent.content not in page.locator(".chat").inner_text()
    else:
        expect(page.locator(".chat")).to_contain_text(sent.content)
    delivery = snapshot()
    history_response = page.request.get(base + f"/v1/groups/{group_b.id}/history?limit=50")
    assert history_response.ok
    history = history_response.json()
    assert any(m["id"] == sent.id for m in history["messages"])
    screenshot = out / f"group-entry-sse-{width}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    page.evaluate("id => CM.features.groups.enter(id)", group_b.id)
    expect(page.locator(".chat")).to_contain_text(sent.content)
    page.wait_for_function("id => window.__groupEntrySources.some(s=>s.url.includes(id) && s.source.readyState===EventSource.OPEN)", arg=group_b.id)
    recovered = snapshot()
    assert page.request.get(base + "/health").json()["runtime_loaded"] is False
    evidence = {
        "head_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
        "captured_at_utc": datetime.now(timezone.utc).isoformat(), "width": width,
        "browser_version": page.context.browser.version, "group_a": group_a.id, "group_b": group_b.id,
        "before_stale_entry_completes": before, "after_stale_entry_completes": after,
        "after_b_publish": delivery, "after_reenter_b": recovered,
        "persisted_b_event_id": sent.id, "published_sequence": sequence,
        "history_contains_b_event": True, "runtime_loaded": False,
        "stale_group_subscription": stale, "screenshot": screenshot.name,
        "source_hashes": {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in (
            "src/character_memory/web/groups.js", "src/character_memory/web/app.js")},
    }
    (out / f"group-entry-sse-{width}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    if stale:
        raise StaleGroupSubscription("Late group entry replaced the current room's native SSE subscription")
