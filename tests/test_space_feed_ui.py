from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_space_feed_uses_card_hierarchy_and_separate_engagement_comments():
    script = (WEB / "space.js").read_text(encoding="utf-8")
    css = (WEB / "space.css").read_text(encoding="utf-8")

    for token in [
        "space-author-block",
        "space-author-meta",
        "space-engagement",
        "space-engagement-counts",
        "space-comments-head",
        "space-comment-avatar",
        "space-comment-body",
    ]:
        assert token in script
        assert f".{token}" in css

    assert "space-like-proof" in script
    assert "成为第一个评论的人" in script
    assert "评论这条动态…" in script


def test_space_feed_is_mobile_first_without_losing_media_or_threading():
    script = (WEB / "space.js").read_text(encoding="utf-8")
    css = (WEB / "space.css").read_text(encoding="utf-8")

    assert "@media(max-width:480px)" in css
    assert ".space-post{border-left:0;border-right:0;border-radius:0" in css
    assert ".space-media-nine" in css
    assert "data-space-thread-toggle" in script
    assert "data-space-comments-toggle" in script
    assert "data-space-reply-comment" in script
    assert "data-space-comment-form" in script


def test_space_script_has_valid_javascript():
    node = shutil.which("node")
    if not node:
        return
    checked = subprocess.run(
        [node, "--check", str(WEB / "space.js")],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr
