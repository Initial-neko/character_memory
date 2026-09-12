from pathlib import Path


def test_voice_assets_are_wired_into_chat_shell():
    html = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    assert 'id="voiceCallButton"' in html
    assert 'id="voiceCallOverlay"' in html
    assert 'id="voiceCallLog"' in html
    assert 'id="voiceMinimizeButton"' in html
    assert 'id="voiceCallDock"' in html
    assert 'id="voiceDockExpandButton"' in html
    assert '当前对话' in html
    assert '/static/voice.css' in html
    assert '/static/voice.js' in html


def test_voice_reuses_existing_async_chat_and_sse_contracts_for_direct_and_group():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert '"/v1/chat/messages"' in script
    assert '/v1/groups/${encodeURIComponent(target.conversationId)}/messages' in script
    assert '"/v1/visual/direct/messages"' in script
    assert '/v1/visual/groups/${encodeURIComponent(target.conversationId)}/messages' in script
    assert 'new EventSource(`/v1/events/stream?' in script
    assert 'source.addEventListener("character_event"' in script
    assert 'source.addEventListener("group_character_event"' in script
    assert '/v1/asr' in script
    assert '/v1/tts' in script


def test_voice_call_remains_half_duplex_without_webrtc():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert '["listening", "recording"].includes(voice.phase)' in script
    assert 'setPhase("speaking"' in script
    assert 'RTCPeerConnection' not in script
    assert 'getDisplayMedia' not in script
    assert 'requestSubmit' not in script


def test_voice_call_pins_target_and_survives_conversation_switches():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'voice.target = target' in script
    assert 'function captureTarget()' in script
    assert 'function isViewingTarget()' in script
    assert 'CM.on("conversationChanged", () => { if (voice.active) renderCallIdentity(); });' in script
    assert 'CM.on("conversationChanged", () => { if (voice.active) stopCall(); });' not in script


def test_voice_call_can_minimize_without_stopping_capture():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'function minimizeCall()' in script
    assert 'function expandCall()' in script
    assert 'dom.overlay?.classList.add("hidden")' in script
    assert 'dom.dock?.classList.remove("hidden")' in script
    minimize_body = script.split('function minimizeCall()', 1)[1].split('function expandCall()', 1)[0]
    assert 'getTracks' not in minimize_body
    assert 'audioContext' not in minimize_body


def test_voice_group_tts_uses_current_character_speaker_and_avatar():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'voice.queue.push({text, characterId, messageId:data.id})' in script
    assert 'voice.currentSpeakerId = item.characterId' in script
    assert 'const speakerId = stableSpeakerId(item.characterId)' in script
    assert 'speaker_id:speakerId' in script
    assert 'profile?.avatar_url' in script
    assert 'voice-call-avatar-image' in script


def test_voice_exposes_latency_breakdown_in_ui():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'ASR ${Math.round(m.asr)}ms' in script
    assert 'LLM ${Math.round(m.llm)}ms' in script
    assert 'TTS ${Math.round(m.tts)}ms' in script
    assert '总计 ${Math.round(m.total)}ms' in script


def test_voice_keeps_live_call_text_visible_and_direct_chat_sync_is_scoped():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'appendCallLog("user"' in script
    assert 'appendCallLog("assistant"' in script
    assert 'voice.target?.scope === "direct" && isViewingTarget()' in script
    assert 'CM.mergeDirectMessage(CM.directEventToMessage(data))' in script
    assert 'CM.features.groups?.reconcileLatest?.(target.conversationId)' in script


def test_voice_css_has_explicit_contrast_and_dock_styles():
    css = Path("src/character_memory/web/voice.css").read_text(encoding="utf-8")
    assert '.voice-call-card' in css and 'color: #f6f7fb' in css
    assert '.voice-call-dock {' in css
    assert '.voice-call-dock-avatar' in css
    assert '.voice-call-avatar-image' in css


def test_voice_does_not_modify_core_controller_ownership():
    app = Path("src/character_memory/web/app.js").read_text(encoding="utf-8")
    voice = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'id="voiceCallButton"' not in app
    assert 'CM.registerFeature("voice"' in voice
