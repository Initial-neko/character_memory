from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "character_memory"
WEB = SRC / "web"


def test_server_installs_group_autonomous_visual_after_async_routes():
    server = (SRC / "server.py").read_text(encoding="utf-8")
    assert "install_group_autonomous_visual(app)" in server
    assert server.index("attach_async_routes(app)") < server.index("install_group_autonomous_visual(app)")


def test_group_visual_reuses_generate_image_contract_without_runtime_monkey_patch():
    visual = (SRC / "group_autonomous_visual.py").read_text(encoding="utf-8")
    service = (SRC / "application" / "group_conversation_service.py").read_text(encoding="utf-8")
    assert "submit_group_image(" in visual
    assert "GroupConversationService._react_member =" not in visual
    assert "group_service_module.compile_context =" not in visual
    assert "allow_generate_image=(not autonomous and direct_visual_available())" in service
    assert "ActionType.GENERATE_IMAGE" in service
    assert 'actor_type="CHARACTER"' in visual
    assert '"image_id": asset.id' in visual
    assert '"media_id": asset.id' in visual
    assert 'hub.publish(group_channel(group.id), "group_character_event"' in visual
    assert "VisualPromptPlanner(runtime.model).compile_prompt" in visual


def test_chat_timestamp_formatter_shows_month_day_and_seconds():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    formatter = (WEB / "time_format.js").read_text(encoding="utf-8")
    assert '/static/time_format.js' in index
    assert index.index('/static/app.js') < index.index('/static/time_format.js') < index.index('/static/groups.js')
    assert "d.getMonth() + 1" in formatter
    assert "d.getDate()" in formatter
    assert "d.getSeconds()" in formatter
    assert "${pad(d.getMonth() + 1)}-${pad(d.getDate())}" in formatter
