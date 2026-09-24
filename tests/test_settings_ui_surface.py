from pathlib import Path


def test_settings_ui_does_not_render_diagnostic_schema_fields():
    root = Path(__file__).resolve().parents[1]
    script = (root / "src" / "character_memory" / "web" / "settings.js").read_text(encoding="utf-8")

    assert 'if (buckets.advanced.length)' in script
    assert 'buckets.diagnostic' not in script
    assert 'Detailed Dev' in script
    assert "后端默认值" in script


def test_settings_page_points_low_level_runtime_tuning_to_detailed_dev():
    root = Path(__file__).resolve().parents[1]
    html = (root / "src" / "character_memory" / "web" / "settings.html").read_text(encoding="utf-8")

    assert "底层轮询、超时、路径等参数使用后端默认值" in html
    assert "Dev Console 的 Detailed Dev" in html
