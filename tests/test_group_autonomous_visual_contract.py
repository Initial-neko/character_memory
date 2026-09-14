from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "character_memory"
WEB = SRC / "web"


def test_server_installs_group_autonomous_visual_after_async_routes():
    server = (SRC / "server.py").read_text(encoding="utf-8")
    assert "install_group_autonomous_visual(app)" in server
    assert server.index("attach_async_routes(app)") < server.index("install_group_autonomous_visual(app)")


def test_group_visual_reuses_generate_image_contract_and_persists_group_media():
    source = (SRC / "group_autonomous_visual.py").read_text(encoding="utf-8")
    assert 'kwargs["allow_generate_image"] = bool(direct_visual_available())' in source
    assert "ActionType.GENERATE_IMAGE.value" in source
    assert '"actor_type="CHARACTER"' not in source  # keyword syntax is not serialized text
    assert 'actor_type="CHARACTER"' in source
    assert '"image_id": asset.id' in source
    assert '"media_id": asset.id' in source
    assert 'hub.publish(group_channel(group.id), "group_character_event"' in source
    assert "VisualPromptPlanner(runtime.model).compile_prompt" in source


def test_chat_timestamp_formatter_shows_month_day_and_seconds():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    formatter = (WEB / "time_format.js").read_text(encoding="utf-8")
    assert '/static/time_format.js' in index
    assert index.index('/static/app.js') < index.index('/static/time_format.js') < index.index('/static/groups.js')
    assert "d.getMonth() + 1" in formatter
    assert "d.getDate()" in formatter
    assert "d.getSeconds()" in formatter
    assert "${pad(d.getMonth() + 1)}-${pad(d.getDate())}" in formatter
