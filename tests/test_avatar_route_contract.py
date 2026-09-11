from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_avatar_routes_do_not_expose_general_web_tools_to_chat_runtime():
    routes = (ROOT / "src" / "character_memory" / "avatar_web.py").read_text(encoding="utf-8")
    search = (ROOT / "src" / "character_memory" / "search.py").read_text(encoding="utf-8")
    runtime = (ROOT / "src" / "character_memory" / "runtime" / "person_runtime.py").read_text(encoding="utf-8")

    assert 'avatar_search.search(' in routes
    assert 'search_web' not in routes
    assert 'WebFetcher' in search
    assert 'web_search is reserved for a later phase' in search
    assert 'web_fetch is reserved for a later phase' in search
    assert 'SearchProvider' not in runtime
    assert 'WebFetcher' not in runtime
