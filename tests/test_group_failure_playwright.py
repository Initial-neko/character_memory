"""Rendered Group feedback, using real history and injected SSE notifications."""
from __future__ import annotations

from test_p0_17b_playwright import _open, realtime_server  # shared real HTTP server fixture
import os

import pytest

if os.getenv("RUN_PLAYWRIGHT") == "1":
    from playwright.sync_api import expect


pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT") != "1", reason="dedicated browser smoke job"),
]


def test_group_error_survives_idle_and_history_redraw(page, realtime_server):
    # Retain the native source so the test can deliver deterministic provider
    # failure notifications without depending on a live model's failure mode.
    page.add_init_script("""(() => {
      const Native = window.EventSource;
      window.EventSource = class extends Native {
        constructor(url, options) {
          super(url, options);
          if (String(url).includes('scope=group')) window.testGroupSource = this;
        }
      };
    })();""")
    _open(page, realtime_server)
    group_id = page.evaluate("""async () => {
      const members = (await (await fetch('/v1/characters')).json()).characters.slice(0, 2);
      const response = await fetch('/v1/groups', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:'Failure feedback smoke',member_ids:members.map(item=>item.id)})});
      const id = (await response.json()).group.id;
      await CM.features.groups.loadGroups();
      await CM.features.groups.enter(id);
      return id;
    }""")
    page.wait_for_function("() => window.testGroupSource?.readyState === EventSource.OPEN")
    page.evaluate("""() => {
      const send = (type, data) => testGroupSource.dispatchEvent(new MessageEvent(type, {data:JSON.stringify(data)}));
      send('reaction_status', {state:'typing',watermark:10});
      send('reaction_error', {watermark:10,message:'provider unavailable'});
      send('reaction_status', {state:'idle',watermark:10});
    }""")
    error = page.locator('.group-reaction-error')
    expect(error).to_be_visible()
    expect(error).to_have_text('群聊生成失败：provider unavailable')
    expect(page.locator('.group-pending-note')).to_have_count(0)
    page.evaluate("async id => CM.features.groups.reconcileLatest(id)", group_id)
    expect(error).to_be_visible()
    page.evaluate("""() => {
      const send = (type, data) => testGroupSource.dispatchEvent(new MessageEvent(type, {data:JSON.stringify(data)}));
      send('reaction_status', {state:'queued',watermark:11});
      send('reaction_error', {watermark:10,message:'late old failure'});
      send('reaction_status', {state:'typing',watermark:11});
    }""")
    expect(error).to_have_count(0)
    expect(page.locator('.group-pending-note')).to_be_visible()
