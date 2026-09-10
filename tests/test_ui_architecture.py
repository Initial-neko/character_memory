from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_index_loads_responsibility_modules_not_version_override_chain():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    for name in ["app.js", "persona.js", "unread.js", "stickers.js", "images.js", "groups.js", "intent.js"]:
        assert f'/static/{name}' in index
    for name in ["p0_5.js", "p0_6.js", "p0_7.js", "p0_8.js", "p0_11.js"]:
        assert f'/static/{name}' not in index
        assert not (WEB / name).exists()


def test_core_is_only_submit_owner_and_features_do_not_monkey_patch_core_functions():
    core = (WEB / "app.js").read_text(encoding="utf-8")
    assert core.count('addEventListener("submit"') == 1
    assert "CM.submitCurrentText" in core
    for name in ["persona.js", "unread.js", "stickers.js", "images.js", "groups.js", "intent.js"]:
        text = (WEB / name).read_text(encoding="utf-8")
        assert 'addEventListener("submit"' not in text
        assert "stopImmediatePropagation" not in text
        assert " = function " not in text


def test_group_text_sticker_and_image_are_all_routed_by_conversation_mode():
    core = (WEB / "app.js").read_text(encoding="utf-8")
    stickers = (WEB / "stickers.js").read_text(encoding="utf-8")
    images = (WEB / "images.js").read_text(encoding="utf-8")
    groups = (WEB / "groups.js").read_text(encoding="utf-8")

    assert 'if (CM.isGroupConversation()) return CM.features.groups?.sendText?.(message);' in core
    assert 'if (CM.isGroupConversation()) return CM.features.groups?.sendSticker?.(sticker);' in stickers
    assert 'if (CM.isGroupConversation()) return CM.features.groups?.sendImage?.(currentDraft, caption);' in images
    assert '/v1/groups/${encodeURIComponent(groupId)}/chat' in groups


def test_group_routes_reuse_create_api_application_runtime_instead_of_building_another_bundle():
    group_web = (ROOT / "src" / "character_memory" / "group_web.py").read_text(encoding="utf-8")
    api = (ROOT / "src" / "character_memory" / "api.py").read_text(encoding="utf-8")

    assert "GroupRuntimeManager" not in group_web
    assert "build_app(" not in group_web
    assert 'app.state, "character_memory"' in group_web
    assert "app.state.character_memory" in api
    assert "access.get_bundle()" in group_web
