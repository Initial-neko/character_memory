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


def test_group_async_send_merges_only_accepted_message_and_uses_sse_for_reactions():
    groups = (WEB / "groups.js").read_text(encoding="utf-8")
    assert 'limit:"50"' in groups
    assert "limit=180" not in groups
    assert '/v1/groups/${encodeURIComponent(groupId)}/messages' in groups
    assert "mergeMessage(result.message)" in groups
    assert 'new EventSource(`/v1/events/stream?' in groups
    assert 'source.addEventListener("group_character_event"' in groups
    assert "result.new_messages" not in groups


def test_background_summary_polling_is_throttled():
    unread = (WEB / "unread.js").read_text(encoding="utf-8")
    assert "const summaryPollMs = 10000;" in unread
    assert "if (!document.hidden) refresh()" in unread
