from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_mobile_access_resolver_loads_before_voice_clients() -> None:
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<script src="/static/mobile_access.js" defer></script>' in index
    assert index.index('/static/app.js') < index.index('/static/mobile_access.js')
    assert index.index('/static/mobile_access.js') < index.index('/static/voice.js')
    assert index.index('/static/mobile_access.js') < index.index('/static/dictation.js')
    assert '<link rel="stylesheet" href="/static/mobile_access.css">' in index


def test_tailscale_media_resolution_is_narrow_and_override_compatible() -> None:
    script = (WEB / "mobile_access.js").read_text(encoding="utf-8")
    assert 'hostname.endsWith(".ts.net")' in script
    assert 'locationLike?.protocol === "https:"' in script
    assert 'return `https://${locationLike.hostname}:8443`' in script
    assert 'return "http://127.0.0.1:8001"' in script
    assert 'localStorage.getItem(MEDIA_BASE_KEY)' in script
    assert 'localStorage.setItem(MEDIA_BASE_KEY, defaultMediaBase())' in script
    assert 'navigator.mediaDevices?.getDisplayMedia' in script


def test_existing_voice_clients_share_the_same_media_override_key() -> None:
    voice = (WEB / "voice.js").read_text(encoding="utf-8")
    dictation = (WEB / "dictation.js").read_text(encoding="utf-8")
    key = 'character-memory:media-base-url'
    assert key in voice
    assert key in dictation


def test_tailscale_serve_helper_keeps_backends_private() -> None:
    helper = (ROOT / "scripts" / "tailscale-serve.sh").read_text(encoding="utf-8")
    assert 'serve --https=443 --bg 8000' in helper
    assert 'serve --https=8443 --bg 8001' in helper
    assert 'serve status' in helper
    assert 'serve reset' not in helper
    assert '"$TAILSCALE" funnel' not in helper


def test_mobile_layout_hides_pc_only_controls_and_avoids_ios_input_zoom() -> None:
    css = (WEB / "mobile_access.css").read_text(encoding="utf-8")
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert '@media (max-width: 820px)' in css
    assert '.desktop-only-control { display: none !important; }' in css
    assert 'font-size: 16px' in css
    assert 'env(safe-area-inset-bottom)' in css
    assert 'desktop-only-control' in index
