from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_avatar_assets_are_loaded_and_server_attaches_routes():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    server = (ROOT / "src" / "character_memory" / "server.py").read_text(encoding="utf-8")

    assert '/static/avatars.js' in index
    assert '/static/p0_15.css' in index
    assert "attach_avatar_routes(app)" in server
    assert server.index("attach_avatar_routes(app)") < server.index("attach_group_routes(app")


def test_avatar_ui_uses_search_session_ids_instead_of_posting_arbitrary_image_url():
    avatars = (WEB / "avatars.js").read_text(encoding="utf-8")

    assert "/avatar/search" in avatars
    assert "/avatar/select" in avatars
    assert "search_id:currentSearch.search_id" in avatars
    assert "candidate_id:candidateId" in avatars
    assert "image_url:" not in avatars
    assert "MutationObserver" in avatars
    assert "group-speaker-name" in avatars


def test_example_config_declares_search_and_avatar_storage_without_enabling_future_web_tools():
    config = (ROOT / "config.example.yaml").read_text(encoding="utf-8")

    assert 'search_provider: "brave"' in config
    assert 'search_api_key: ""' in config
    assert 'search_safe_search: "strict"' in config
    assert 'avatar_dir: ""' in config
    assert "# web_search_enabled: false" in config
    assert "# web_fetch_enabled: false" in config
