from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_message_sound_module_is_loaded() -> None:
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<script src="/static/message_sound.js" defer></script>' in index
    assert index.index('/static/app.js') < index.index('/static/message_sound.js')
    assert index.index('/static/message_sound.js') < index.index('/static/groups.js')


def test_message_sound_is_lightweight_and_best_effort() -> None:
    sound = (WEB / "message_sound.js").read_text(encoding="utf-8")
    assert "createOscillator" in sound
    assert "createGain" in sound
    assert 'oscillator.type = "sine"' in sound
    assert "0.095" in sound
    assert "0.028" in sound
    assert 'document.addEventListener("pointerdown", primeAudio' in sound
    assert 'document.addEventListener("keydown", primeAudio' in sound


def test_message_sound_only_arms_after_history_baseline() -> None:
    sound = (WEB / "message_sound.js").read_text(encoding="utf-8")
    assert 'CM.on("historyLoaded", seedCurrentConversation)' in sound
    assert 'CM.on("conversationChanged", seedCurrentConversation)' in sound
    assert "baselineReady" in sound
    assert 'row.matches(".message-row.assistant[data-message-id]")' in sound
    assert "numericId > state.maxNumericId" in sound


def test_message_sound_suppresses_voice_calls_and_duplicate_or_older_rows() -> None:
    sound = (WEB / "message_sound.js").read_text(encoding="utf-8")
    assert "state.seen.has(id)" in sound
    assert "if (!fresh || !newer) return" in sound
    assert "CM.features.voice?.state?.active" in sound
