from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_dev_defaults_to_simple_and_exposes_detailed_toggle():
    html = (WEB / "dev.html").read_text(encoding="utf-8")
    css = (WEB / "dev.css").read_text(encoding="utf-8")
    script = (WEB / "dev_mode.js").read_text(encoding="utf-8")

    assert 'data-dev-mode="simple"' in html
    assert 'id="devModeToggle"' in html
    assert 'id="devModeHint"' in html
    assert "/static/dev_mode.js" in html
    assert 'data-dev-surface="detailed"' in html
    assert 'body[data-dev-mode="simple"] [data-dev-surface="detailed"]' in css
    assert '"character-memory:dev-mode"' in script
    assert '"simple"' in script and '"detailed"' in script


def test_simple_dev_keeps_only_high_frequency_space_controls_on_first_surface():
    html = (WEB / "dev.html").read_text(encoding="utf-8")

    assert 'id="spaceIntervalMinutes"' in html
    assert 'id="spaceMaxPostsPerDay"' in html
    assert 'value="120"' in html
    assert 'value="3"' in html
    assert "到点只是让人物判断一次，不代表一定发动态" in html
    assert "每个人物每天的发布上限" in html

    # Low-frequency plumbing remains available in Detailed Dev instead of
    # becoming normal operating controls.
    assert 'id="spacePollSeconds"' in html
    assert 'id="spaceAudienceSize"' in html
    assert 'data-level="advanced"' in html


def test_simple_dev_keeps_llm_usage_summary_but_hides_detailed_tools():
    html = (WEB / "dev.html").read_text(encoding="utf-8")
    assert 'id="llmUsageCard"' in html
    assert 'data-dev-surface="detailed"' in html
    assert 'id="refreshLlmUsage"' in html
