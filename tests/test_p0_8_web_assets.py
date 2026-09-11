from pathlib import Path


def test_image_module_is_loaded_and_packaged():
    root = Path(__file__).parents[1]
    web = root / "src" / "character_memory" / "web"
    index = (web / "index.html").read_text(encoding="utf-8")
    js = web / "images.js"
    css = web / "p0_8.css"
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert '/static/p0_8.css' in index
    assert '/static/images.js' in index
    assert '/static/p0_8.js' not in index
    assert js.is_file()
    assert css.is_file()
    assert 'web/*.js' in pyproject
    assert 'web/*.css' in pyproject
    text = js.read_text(encoding="utf-8")
    css_text = css.read_text(encoding="utf-8")
    assert 'image/jpeg", "image/png", "image/gif", "image/webp' in text
    assert 'CM.registerFeature("images"' in text
    assert 'event.key !== "Enter"' in text
    assert "event.shiftKey" in text
    assert "event.isComposing" in text
    assert 'event.target.closest(".image-bubble img")' in text
    assert "image-lightbox" in text
    assert ".image-lightbox" in css_text
    assert "cursor: zoom-in" in css_text
