from dataclasses import replace
import json

import pytest

from character_memory.runtime.capability_discovery import CapabilityAvailability, available_capabilities


def role(tmp_path, grant=None):
    folder = tmp_path / "personas" / "a"
    folder.mkdir(parents=True)
    persona = folder / "persona.yaml"
    persona.write_text("id: a\n", encoding="utf-8")
    if grant:
        (folder / "capabilities.yaml").write_text(f"capabilities: [{grant}]\n", encoding="utf-8")
    return {"id": "a", "persona_path": str(persona)}


def ids(result):
    return [item.id for item in result]


def test_world_discovery_exposes_only_configured_existing_readers():
    profile = {"id": "a"}
    state = CapabilityAvailability(web_search=True, rss_reading=True)
    result = available_capabilities(profile, channel="WORLD", availability=state)
    assert ids(result) == ["WEB_SEARCH", "READ_RSS"]
    assert ids(available_capabilities(profile, channel="WORLD", availability=CapabilityAvailability())) == []
    query = result[0].arguments_schema
    assert query["additionalProperties"] is False
    assert query["required"] == ["query"]
    assert query["properties"]["query"]["maxLength"] == 240
    assert not {"character_id", "channel", "constraints", "role_grant"}.intersection(query["properties"])
    assert result[0].backend == "direct"
    assert result[0].skills == ()


@pytest.mark.parametrize("channel", ["DIRECT", "GROUP", "SPACE", "VISUAL", "UNKNOWN", "world"])
def test_readers_are_not_exposed_across_channels(channel):
    state = CapabilityAvailability(web_search=True, rss_reading=True, claude=True)
    assert available_capabilities({"id": "a"}, channel=channel, availability=state) == ()


@pytest.mark.parametrize("profile", [None, {}, {"id": ""}, {"id": "a", "archived_at": None}])
def test_invalid_and_archived_profiles_fail_closed(profile):
    state = CapabilityAvailability(web_search=True, rss_reading=True)
    assert available_capabilities(profile, channel="WORLD", availability=state) == ()


def test_no_opportunity_allowance_hides_all_capabilities():
    state = CapabilityAvailability(web_search=True, rss_reading=True, remaining_calls=0)
    assert available_capabilities({"id": "a"}, channel="WORLD", availability=state) == ()


def test_claude_requires_correct_role_grant_and_available_runner(tmp_path):
    profile = role(tmp_path, "claude_task")
    state = CapabilityAvailability(claude=True, direct_task=True)
    result = available_capabilities(profile, channel="DIRECT", availability=state)
    assert ids(result) == ["CLAUDE_TASK"]
    assert result[0].backend == "claude"
    assert available_capabilities(profile, channel="DIRECT", availability=replace(state, claude=False)) == ()
    assert available_capabilities(profile, channel="DIRECT", availability=replace(state, direct_task=False)) == ()
    assert available_capabilities(profile, channel="GROUP", availability=state) == ()
    world = replace(state, web_search=True, world_backend="claude")
    assert ids(available_capabilities(profile, channel="WORLD", availability=world)) == ["WEB_SEARCH"]
    assert available_capabilities(profile, channel="WORLD", availability=world)[0].backend == "direct"


def test_world_claude_grant_does_not_grant_direct_task(tmp_path):
    profile = role(tmp_path, "claude_research")
    state = CapabilityAvailability(web_search=True, claude=True, direct_task=True, world_backend="claude")
    assert available_capabilities(profile, channel="DIRECT", availability=state) == ()
    result = available_capabilities(profile, channel="WORLD", availability=state)
    assert result[0].backend == "claude"
    assert available_capabilities(profile, channel="WORLD", availability=replace(state, claude=False)) == ()


def test_invalid_sidecar_cannot_expose_claude_or_skills(tmp_path):
    profile = role(tmp_path)
    (tmp_path / "personas/a/capabilities.yaml").write_text("capabilities: [shell]\n", encoding="utf-8")
    state = CapabilityAvailability(claude=True, direct_task=True)
    assert available_capabilities(profile, channel="DIRECT", availability=state) == ()


def test_discovery_returns_fresh_schemas_without_touching_providers():
    state = CapabilityAvailability(web_search=True)
    first = available_capabilities({"id": "a"}, channel="WORLD", availability=state)
    first[0].arguments_schema["properties"]["query"]["maxLength"] = 999
    second = available_capabilities({"id": "a"}, channel="WORLD", availability=state)
    assert second[0].arguments_schema["properties"]["query"]["maxLength"] == 240
    assert json.dumps(second[0].arguments_schema)


def test_authorized_skill_snapshot_disappears_after_grant_revocation(tmp_path):
    profile = role(tmp_path, "claude_task")
    folder = tmp_path / "personas/a"
    skill = folder / "skills/research"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: research\ndescription: Public research\n---\nReview public evidence.\n", encoding="utf-8")
    config = folder / "capabilities.yaml"
    config.write_text("capabilities: [claude_task]\nskills: [personal/research]\n", encoding="utf-8")
    state = CapabilityAvailability(claude=True, direct_task=True)
    result = available_capabilities(profile, channel="DIRECT", availability=state)
    assert result[0].skills[0]["name"] == "research"
    assert len(result[0].skills[0]["sha256"]) == 64
    config.write_text("capabilities: []\n", encoding="utf-8")
    assert available_capabilities(profile, channel="DIRECT", availability=state) == ()


@pytest.mark.parametrize("changes", [
    {"web_search": "true"}, {"remaining_calls": True},
    {"remaining_calls": -1}, {"world_backend": "shell"},
])
def test_availability_rejects_ambiguous_server_snapshots(changes):
    with pytest.raises(ValueError):
        CapabilityAvailability(**changes)


def test_rss_description_matches_existing_executor_semantic_parameters():
    from character_memory.runtime.capability_execution import CapabilityExecutor, CapabilityRequest
    state = CapabilityAvailability(rss_reading=True)
    description, = available_capabilities({"id": "a"}, channel="WORLD", availability=state)
    schema = description.arguments_schema
    assert schema["required"] == ["items"]
    assert schema["properties"]["items"]["items"]["required"] == ["item_id", "source_generation"]
    request = CapabilityRequest("r:1", "a", "o:1", None, "WORLD", description.id,
        {"items": [{"item_id": 1, "source_generation": 0}]},
        {"max_items": 2, "max_chars": 12000, "max_calls": 1})
    assert CapabilityExecutor._denial(request) == ""


def test_schema_rejects_blank_query_and_documents_unique_rss_items():
    import re
    state = CapabilityAvailability(web_search=True, rss_reading=True)
    web, rss = available_capabilities({"id": "a"}, channel="WORLD", availability=state)
    query = web.arguments_schema["properties"]["query"]
    assert re.search(query["pattern"], " \t\n") is None
    assert re.search(query["pattern"], "public evidence")
    items = rss.arguments_schema["properties"]["items"]
    assert items["uniqueItems"] is True
    assert "item_id" in items["description"]
