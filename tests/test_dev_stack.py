from pathlib import Path


def test_stack_entrypoint_and_runtime_ports_are_declared():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert 'character-stack = "character_memory.dev_stack:main"' in pyproject
    assert 'character-tts-lab = "character_memory.tts_lab:main"' in pyproject
    assert "127.0.0.1:8000/health" in script
    assert "127.0.0.1:8001/health" in script
    assert "127.0.0.1:8002/health" in script
    assert "127.0.0.1:9002/health" in script
    assert '"character_memory.media_bootstrap"' in script
    assert '"character_memory.dev_server"' in script
    assert '"character_memory.tts_lab"' in script
    assert 'choices=("dev", "chat", "tts")' in script


def test_dev_console_is_the_unified_front_door():
    html = Path("src/character_memory/web/dev.html").read_text(encoding="utf-8")
    assert 'href="http://127.0.0.1:8000"' in html
    assert 'href="http://127.0.0.1:8001/health"' in html
    assert "打开 Chat :8000" in html
