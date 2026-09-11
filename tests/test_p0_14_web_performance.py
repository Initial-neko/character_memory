from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_direct_chat_does_not_reload_full_history_after_success():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "let succeeded = false;" in app
    assert "succeeded = true;" in app
    assert "if (!succeeded)" in app
    assert "Avoid an immediate 180-message GET" in app


def test_group_chat_does_not_reload_full_history_after_success():
    groups = (WEB / "groups.js").read_text(encoding="utf-8")
    assert "let succeeded = false;" in groups
    assert "if (!succeeded && CM.isGroupConversation()" in groups
    assert "Avoid an" in groups and "duplicate GET" in groups


def test_background_summary_polling_is_throttled():
    unread = (WEB / "unread.js").read_text(encoding="utf-8")
    assert "const summaryPollMs = 10000;" in unread
    assert "if (!document.hidden) refresh()" in unread


def test_chat_rhythm_keeps_short_delay_cap():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "Math.min(base + jitter, 600)" in app
    assert "Math.min(base + jitter, 1200)" not in app
