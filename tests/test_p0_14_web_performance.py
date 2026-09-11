from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_direct_chat_history_is_bounded_to_cursor_pages():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "/v1/chat/history-page" in app
    assert 'limit:"50"' in app
    assert "limit=180" not in app
    assert "nextBeforeId" in app
    assert "data-load-older-direct" in app


def test_group_chat_merges_current_turn_and_only_reconciles_history_on_failure():
    groups = (WEB / "groups.js").read_text(encoding="utf-8")
    assert 'limit:"50"' in groups
    assert "limit=180" not in groups
    assert "result.new_messages" in groups
    assert "group history reconcile failed" in groups
    assert "catch (error)" in groups
    assert "await loadHistory().catch" in groups


def test_background_summary_polling_is_throttled():
    unread = (WEB / "unread.js").read_text(encoding="utf-8")
    assert "const summaryPollMs = 10000;" in unread
    assert "if (!document.hidden) refresh()" in unread
