from pathlib import Path


def test_stack_entrypoint_and_runtime_ports_are_declared():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert 'character-stack = "character_memory.dev_stack:main"' in pyproject
    assert 'character-settings = "character_memory.settings_server:main"' in pyproject
    assert 'character-tts-lab = "character_memory.tts_lab:main"' in pyproject
    assert "127.0.0.1:8000/health" in script
    assert "127.0.0.1:8001/health" in script
    assert "127.0.0.1:8002/health" in script
    assert "127.0.0.1:8003/health" in script
    assert '"http://127.0.0.1:9002/tts"' in script
    assert '"http://127.0.0.1:9002/health"' not in script
    assert '"character_memory.media_bootstrap"' in script
    assert '"character_memory.dev_server"' in script
    assert '"character_memory.settings_server"' in script
    assert '"character_memory.tts_lab"' in script
    assert 'choices=("dev", "chat", "settings", "tts")' in script


def test_dev_console_is_linked_from_settings_center():
    html = Path("src/character_memory/web/settings.html").read_text(encoding="utf-8")
    assert 'href="http://127.0.0.1:8000"' in html
    assert 'href="http://127.0.0.1:8002/dev"' in html
    assert 'href="http://127.0.0.1:9002/tts"' in html
    assert "Settings Center" in html


def test_configured_tts_provider_accepts_edge_and_gsv(tmp_path):
    from character_memory.dev_stack import _configured_tts_provider

    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "edge"\n', encoding="utf-8")
    assert _configured_tts_provider(str(config)) == "edge"

    config.write_text('tts_provider: "gsv"\n', encoding="utf-8")
    assert _configured_tts_provider(str(config)) == "gsv"


def test_qwen3_is_not_a_formal_stack_provider(tmp_path):
    from character_memory.dev_stack import _configured_tts_provider

    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "qwen3"\n', encoding="utf-8")
    try:
        _configured_tts_provider(str(config))
    except ValueError as exc:
        assert "Qwen3-TTS is reserved for future voice-design tooling" in str(exc)
    else:
        raise AssertionError("qwen3 must not be accepted as a formal TTS provider")


def test_dev_stack_declares_gsv_sidecar_startup():
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert '"GSV-TTS-Lite Runtime"' in script
    assert '"http://127.0.0.1:9014/health"' in script
    assert '"GSV_TTS_GPT_MODEL"' in script
    assert '"GSV_TTS_REF_AUDIO"' in script
    assert '"CHARACTER_TTS_GSV_BASE"' in script


def test_dev_stack_keeps_gsv_health_checkable_without_preloading():
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert '"Qwen3-TTS Runtime"' not in script
    assert 'gsv_available = gsv_python.is_file() and not missing_gsv' in script
    assert 'gsv_env["GSV_TTS_PRELOAD"] = "1" if tts_provider == "gsv" else "0"' in script
    assert 'gsv_env["GSV_TTS_DEVICE"] = tts_device' in script
    assert 'gsv_env.setdefault("GSV_TTS_VOICE"' in script
    assert 'media_env["CHARACTER_MEDIA_TTS_DEVICE"] = tts_device' in script
