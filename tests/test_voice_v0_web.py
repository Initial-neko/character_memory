from pathlib import Path


def test_voice_assets_are_wired_into_chat_shell():
    html = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    assert 'id="voiceCallButton"' in html
    assert 'id="voiceCallOverlay"' in html
    assert '/static/voice.css' in html
    assert '/static/voice.js' in html


def test_voice_v0_reuses_existing_async_chat_and_sse_contract():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'fetch("/v1/chat/messages"' in script
    assert 'new EventSource(`/v1/events/stream?' in script
    assert '/v1/asr' in script
    assert '/v1/tts' in script
    assert 'CM.mergeDirectMessage' in script


def test_voice_v0_is_direct_chat_only_and_half_duplex():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'CM.isGroupConversation()' in script
    assert '["listening", "recording"].includes(voice.phase)' in script
    assert 'setPhase("speaking"' in script
    # V0 deliberately does not add WebRTC/full-duplex or a second agent runtime.
    assert 'RTCPeerConnection' not in script
    assert 'getDisplayMedia' not in script
    assert 'requestSubmit' not in script


def test_voice_v0_exposes_latency_breakdown_in_ui():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'ASR ${Math.round(m.asr)}ms' in script
    assert 'LLM ${Math.round(m.llm)}ms' in script
    assert 'TTS ${Math.round(m.tts)}ms' in script
    assert '总计 ${Math.round(m.total)}ms' in script


def test_voice_v0_does_not_modify_core_controller_ownership():
    app = Path("src/character_memory/web/app.js").read_text(encoding="utf-8")
    voice = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'id="voiceCallButton"' not in app
    assert 'CM.registerFeature("voice"' in voice
