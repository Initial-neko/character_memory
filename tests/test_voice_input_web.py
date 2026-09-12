from pathlib import Path
import shutil
import subprocess


def test_chat_composer_exposes_asr_dictation_button():
    html = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    assert 'id="voiceInputButton"' in html
    assert '/static/dictation.js' in html
    assert '语音转文字' in html


def test_dictation_uses_media_asr_and_does_not_auto_send_or_tts():
    script = Path("src/character_memory/web/dictation.js").read_text(encoding="utf-8")
    assert '/v1/asr' in script
    assert '/v1/tts' not in script
    assert '/v1/chat/messages' not in script
    assert 'insertTranscript(text)' in script
    assert 'CM.dom.input' in script
    assert 'CM.isGroupConversation()' not in script


def test_dictation_and_call_do_not_capture_microphone_together():
    script = Path("src/character_memory/web/dictation.js").read_text(encoding="utf-8")
    assert 'CM.features.voice?.state?.active' in script
    assert 'voiceCallButton' in script
    assert 'recognize:false' in script


def test_dictation_script_is_valid_javascript_when_node_is_available():
    node = shutil.which("node")
    if not node:
        return
    path = Path("src/character_memory/web/dictation.js")
    checked = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
