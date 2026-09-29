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


def test_space_formal_tuning_is_a_closed_runtime_override_not_first_screen_config():
    html = (WEB / "dev.html").read_text(encoding="utf-8")

    # The tools still exist for soak/scheduler validation.
    assert 'id="spaceIntervalMinutes"' in html
    assert 'id="spaceMaxPostsPerDay"' in html
    assert 'id="spacePollSeconds"' in html
    assert 'id="spaceAudienceSize"' in html
    assert 'id="applySpaceConfig"' in html

    # But the surface names the ownership explicitly: this is temporary,
    # non-persistent Runtime state rather than a second Settings Center.
    assert "Session Override（仅当前 Runtime）" in html
    assert "不写 config.yaml" in html
    assert "正式配置在 Settings" in html


def test_simple_dev_keeps_llm_usage_summary_but_hides_detailed_tools():
    html = (WEB / "dev.html").read_text(encoding="utf-8")
    assert 'id="llmUsageCard"' in html
    assert 'data-dev-surface="detailed"' in html
    assert 'id="refreshLlmUsage"' in html
