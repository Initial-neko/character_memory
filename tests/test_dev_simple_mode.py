from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_dev_console_defaults_to_simple_mode_with_explicit_detailed_toggle():
    html = (WEB / "dev.html").read_text(encoding="utf-8")
    css = (WEB / "dev.css").read_text(encoding="utf-8")
    script = (WEB / "dev_mode.js").read_text(encoding="utf-8")

    assert 'data-dev-mode="simple"' in html
    assert 'id="devModeToggle"' in html
    assert 'id="devModeHint"' in html
    assert "/static/dev_mode.js" in html
    assert 'data-dev-surface="detailed"' in html
    assert 'body[data-dev-mode="simple"] [data-dev-surface="detailed"]' in css
    assert 'details.level-group[data-level="advanced"]' in css
    assert 'details.level-group[data-level="diagnostic"]' in css
    assert 'details.debug-output' in css
    assert '"character-memory:dev-mode"' in script
    assert '"simple"' in script and '"detailed"' in script


def test_only_high_frequency_scheduler_knobs_live_on_simple_dev_surface():
    html = (WEB / "dev.html").read_text(encoding="utf-8")

    assert html.count('id="spaceIntervalMinutes"') == 1
    assert html.count('id="spaceMaxPostsPerDay"') == 1
    assert html.count('id="groupAutonomyInterval"') == 1
    assert "发动态机会间隔" in html
    assert "每天最多动态" in html
    assert "交流机会间隔" in html
    assert "只热应用到当前运行中的 Character Runtime" in html
    assert "立即跑一次判断，不改变正式 Scheduler" in html

    # Transport/plumbing knobs still exist, but only inside advanced/detailed UI.
    assert 'id="spacePollSeconds"' in html
    assert 'id="spaceAudienceSize"' in html
    assert 'id="groupAutonomyPollSeconds"' in html


def test_llm_usage_summary_remains_visible_in_simple_mode_but_tables_are_detailed():
    html = (WEB / "dev.html").read_text(encoding="utf-8")
    common = html.split('<div class="llm-usage-common"', 1)[1].split(
        '<details class="level-group" data-level="diagnostic"', 1
    )[0]
    assert 'id="usageRequests"' in common
    assert 'id="usageTotalTokens"' in common
    assert 'id="refreshLlmUsage"' in common
    assert "按功能归因" not in common


def test_dev_mode_script_has_valid_javascript():
    node = shutil.which("node")
    if not node:
        return
    checked = subprocess.run(
        [node, "--check", str(WEB / "dev_mode.js")],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr
