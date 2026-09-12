from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_direct_reconcile_contract_is_loaded_and_merges_by_event_id():
    script = (WEB / "realtime_reconcile.js").read_text(encoding="utf-8")
    index = (WEB / "index.html").read_text(encoding="utf-8")

    assert 'source.addEventListener("open"' in script
    assert "/v1/chat/history-page" in script
    assert "new Map(CM.state.directHistory.messages" in script
    assert "__cmReconcileAttached" in script
    assert '/static/realtime_reconcile.js' in index


def test_group_reconcile_and_mentions_use_one_authoritative_group_state():
    groups = (WEB / "groups.js").read_text(encoding="utf-8")
    mentions = (WEB / "mentions.js").read_text(encoding="utf-8")

    assert 'source.addEventListener("open"' in groups
    assert "reconcileLatest(groupId)" in groups
    assert "current" in groups and 'registerFeature("groups"' in groups
    assert "CM.features.groups?.current?.()" in mentions
    assert 'CM.on("conversationChanged", () =>' in mentions
    assert 'await refreshGroups()' not in mentions.split('CM.on("conversationChanged"', 1)[-1]


def test_realtime_javascript_syntax():
    node = shutil.which("node")
    if not node:
        return
    for path in (WEB / "realtime_reconcile.js", WEB / "groups.js", WEB / "mentions.js"):
        subprocess.run([node, "--check", str(path)], check=True, capture_output=True, text=True)
