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


def test_voice_call_keeps_capture_live_during_playback_without_barge_in_or_webrtc():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert '["listening", "recording"].includes(voice.capturePhase)' in script
    assert 'setCapturePhase("listening")' in script
    assert 'setPhase("speaking"' in script
    stop_body = script.split('async function stopCall()', 1)[1].split('dom.button?.addEventListener', 1)[0]
    assert 'audio.pause()' in stop_body
    assert 'audio.onended?.()' in stop_body
    assert 'audio.pause()' not in script.split('function audioFrame', 1)[1].split('async function startCall', 1)[0]
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


def test_voice_call_can_mute_microphone_without_stopping_visual_or_tts():
    index = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    app = Path("src/character_memory/web/app.js").read_text(encoding="utf-8")
    groups = Path("src/character_memory/web/groups.js").read_text(encoding="utf-8")

    assert 'id="voiceMicButton"' in index
    assert "micActive: false" in script
    assert "async function startMicrophone" in script
    assert "async function stopMicrophone" in script
    assert "async function toggleMicrophone" in script
    stop_mic = script.split("async function stopMicrophone", 1)[1].split("async function startMicrophone", 1)[0]
    assert "visualSession?.stop" not in stop_mic
    assert "eventSource?.close" not in stop_mic
    assert "currentAudio" not in stop_mic
    start_call = script.split("async function startCall()", 1)[1].split("async function stopCall()", 1)[0]
    assert "voice.active = true" in start_call
    assert "startMicrophone({throwOnError:false})" in start_call
    assert start_call.index("voice.active = true") < start_call.index("startMicrophone({throwOnError:false})")
    assert "sendTextWithVisual" in script
    assert "CM.features.voice?.sendTextWithVisual?.(message)" in app
    assert "CM.features.voice?.sendTextWithVisual?.(message)" in groups


def test_voice_call_can_minimize_without_stopping_capture():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'function minimizeCall()' in script
    assert 'function expandCall()' in script
    assert 'dom.overlay?.classList.add("hidden")' in script
    assert 'dom.dock?.classList.remove("hidden")' in script
    minimize_body = script.split('function minimizeCall()', 1)[1].split('function expandCall()', 1)[0]
    assert 'getTracks' not in minimize_body
    assert 'audioContext' not in minimize_body
    assert 'visualSession' not in minimize_body


def test_voice_group_tts_uses_current_character_speaker_avatar_and_prefetch_slot():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'voice.queue.push({text, characterId, messageId:data.id, audioPromise:null, audioUrl:null})' in script
    assert 'voice.currentSpeakerId = item.characterId' in script
    assert 'const speakerId = stableSpeakerId(item.characterId)' in script
    assert 'speaker_id:speakerId' in script
    assert 'function prefetchNext()' in script
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


def test_voice_asr_gate_rejects_empty_punctuation_and_low_information_before_queue_or_send():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    assert 'function validateAsrTranscript(raw)' in script
    assert 'reason:"empty"' in script
    assert 'text.replace(/[\\s\\p{P}\\p{S}]/gu, "")' in script
    assert '/\\p{Script=Han}/u.test(text)' in script
    assert 'text.match(/[A-Za-z0-9]/g)' in script
    assert 'latinOrDigitCount >= 2' in script
    assert 'reason:"punctuation_only"' in script
    assert 'reason:"too_short"' in script

    finish_speech = script.split('async function finishSpeech()', 1)[1].split('async function synthesize', 1)[0]
    assert 'const validation = validateAsrTranscript(result.text);' in finish_speech
    invalid_block = finish_speech.split('if (!validation.valid) {', 1)[1].split('const turn = {text:validation.text, visualFrames, asrMs};', 1)[0]
    assert '没有识别到有效内容' in invalid_block
    assert 'setPhase("listening", "正在听…")' in invalid_block
    assert 'return;' in invalid_block
    assert 'sendTranscript' not in invalid_block
    assert finish_speech.index('const validation = validateAsrTranscript(result.text);') < finish_speech.index('const turn = {text:validation.text, visualFrames, asrMs};')


def test_voice_buffers_valid_asr_during_tts_and_submits_after_queue_drain():
    script = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")
    finish_speech = script.split('async function finishSpeech()', 1)[1].split('async function synthesize', 1)[0]
    play_queue = script.split('async function playQueue()', 1)[1].split('function audioFrame', 1)[0]

    assert 'if (voice.playing || voice.queue.length)' in finish_speech
    assert 'voice.pendingTurns.push(turn)' in finish_speech
    assert '已听到，等待对方说完' in finish_speech
    assert 'await flushPendingTurns();' in play_queue
    assert 'await playAudio(url);' in play_queue


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
