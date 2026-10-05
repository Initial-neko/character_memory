"""Unit and source contracts for the opt-in call Live2D presentation.

These do not download Cubism Core, use GPUs or claim visual compatibility.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_memory.live2d_web import model_directory, model_manifest, resolve_model_asset

WEB = Path(__file__).resolve().parents[1] / "src" / "character_memory" / "web"


def test_model_manifest_reads_the_export_without_flattening_texture_paths(tmp_path):
    folder = tmp_path / "rin"
    folder.mkdir()
    (folder / "rin.model3.json").write_text('{"Version":3}', encoding="utf-8")
    texture = folder / "rin.4096" / "texture_00.png"
    texture.parent.mkdir()
    texture.write_bytes(b"png")

    assert model_manifest(tmp_path, "rin") == folder / "rin.model3.json"
    assert resolve_model_asset(tmp_path, "rin", "rin.4096/texture_00.png") == texture
    assert model_manifest(tmp_path, "missing") is None


@pytest.mark.parametrize("value", ["../secrets", "a/../../b", "", ".", "..", r"a\b"])
def test_model_directory_denies_path_escapes(tmp_path, value):
    with pytest.raises(ValueError):
        model_directory(tmp_path, value)


@pytest.mark.parametrize("path", [
    "../secrets.json", "nested/../../secret.moc3", "/etc/passwd",
    r"folder\secret.json", "test.py", "folder//texture.png", "./model3.json",
])
def test_asset_server_denies_non_model_and_traversal_paths(tmp_path, path):
    folder = tmp_path / "rin"
    folder.mkdir(exist_ok=True)
    with pytest.raises(ValueError):
        resolve_model_asset(tmp_path, "rin", path)


def test_asset_symlink_cannot_escape_character_directory(tmp_path):
    folder = tmp_path / "rin"
    folder.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text('{"api_key":"private"}', encoding="utf-8")
    link = folder / "secret.json"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable on this platform")
    with pytest.raises(ValueError):
        resolve_model_asset(tmp_path, "rin", "secret.json")


def test_call_wiring_keeps_live2d_optional_and_separate_from_tts():
    shell = (WEB / "index.html").read_text(encoding="utf-8")
    voice = (WEB / "voice.js").read_text(encoding="utf-8")
    viewer = (WEB / "live2d.js").read_text(encoding="utf-8")
    style = (WEB / "voice.css").read_text(encoding="utf-8")

    assert shell.index("/static/live2d.js") < shell.index("/static/voice.js")
    for marker in ("voiceLive2dButton", "voiceLive2dStage", "voiceLive2dMessage"):
        assert marker in shell and marker in viewer
    assert 'CM.live2d?.setCharacter(' in voice
    assert 'CM.live2d?.pause()' in voice
    assert 'CM.live2d?.resume()' in voice
    assert 'CM.live2d?.stop()' in voice
    assert "audio.play().catch(reject)" in voice  # existing playback remains the owner
    assert "voice-live2d-stage" in style
    assert "Live2DModel.from(url)" in viewer
    assert "createRenderer" in viewer and "destroy()" in viewer
    assert "/static/vendor/live2d/" in viewer
    assert "https://cdn" not in viewer
