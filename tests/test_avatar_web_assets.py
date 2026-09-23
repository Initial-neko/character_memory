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


def test_avatar_ui_uses_llm_hint_and_search_session_ids_without_arbitrary_image_url():
    avatars = (WEB / "avatars.js").read_text(encoding="utf-8")

    assert "/avatar/search" in avatars
    assert "/avatar/select" in avatars
    assert "search_id:currentSearch.search_id" in avatars
    assert "candidate_id:candidateId" in avatars
    assert "image_url:" not in avatars
    assert "MutationObserver" in avatars
    assert "group-speaker-name" in avatars
    assert "data-avatar-hint" in avatars
    assert "让角色决定并搜索" in avatars
    assert "portrait avatar profile picture" not in avatars
    assert "JSON.stringify({hint, limit:12})" in avatars
    assert "visual_intent" in avatars
    assert "AI 搜索词" in avatars
    assert "data-avatar-style" in avatars
    assert "data-avatar-count" in avatars
    assert "avatar-generated-grid" in avatars
    assert "polished_prompt" in avatars
    assert "生成候选头像" in avatars


def test_example_config_declares_searchapi_avatar_storage_and_bounded_world_browser():
    config = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert 'search_provider: "searchapi"' in config
    assert 'search_api_key: ""' not in config
    assert "SEARCHAPI_API_KEY=" in env_example
    assert "BRAVE_SEARCH_API_KEY=" in env_example
    assert 'search_country: "jp"' in config
    assert 'search_language: "zh-cn"' in config
    assert 'search_safe_search: "strict"' in config
    assert 'avatar_dir: ""' in config
    assert '# search_provider: "brave"' in config
    assert 'web_browser_channel: "auto"' in config
    assert "web_browser_timeout_seconds: 20" in config
    assert "space_world_observation_enabled: true" in config
    assert "space_world_max_pages: 2" in config
