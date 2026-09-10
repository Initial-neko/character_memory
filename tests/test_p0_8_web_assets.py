from pathlib import Path


def test_p0_8_assets_are_loaded_and_packaged():
    root = Path(__file__).parents[1]
    index = (root / "src" / "character_memory" / "web" / "index.html").read_text(encoding="utf-8")
    js = root / "src" / "character_memory" / "web" / "p0_8.js"
    css = root / "src" / "character_memory" / "web" / "p0_8.css"
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert '/static/p0_8.css' in index
    assert '/static/p0_8.js' in index
    assert js.is_file()
    assert css.is_file()
    assert 'web/*.js' in pyproject
    assert 'web/*.css' in pyproject
    assert 'image/jpeg,image/png,image/gif,image/webp' in js.read_text(encoding="utf-8")
