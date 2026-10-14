from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from character_memory.character_capabilities import CharacterCapabilities, select_role_research


def _role(tmp_path: Path, name="anime"):
    folder = tmp_path / "personas" / name
    folder.mkdir(parents=True)
    path = folder / "persona.yaml"
    path.write_text("id: "+name+"\n",encoding="utf-8")
    return {"id": name, "persona_path": str(path)}, folder


def test_capability_grant_defaults_to_nothing_and_missing_profile(tmp_path):
    profile, folder = _role(tmp_path)
    assert select_role_research(profile) is None
    assert select_role_research({"id": "anonymous"}) is None
    (folder / "capabilities.yaml").write_text("capabilities: []\n", encoding="utf-8")
    assert select_role_research(profile) is None
    (folder / "capabilities.yaml").write_text("capabilities: [claude_research]\n", encoding="utf-8")
    assert select_role_research(profile) == []


@pytest.mark.parametrize("payload", [
    "capabilities: [unknown]\n",
    "capabilities: [claude_research, claude_research]\n",
    "capabilities: []\nskills: [shared/anime]\n",
    "capabilities: [claude_research]\nskills: [../../config]\n",
    "capabilities: [claude_research]\nextra_permission: write_repo\n",
    "capabilities: [claude_research]\nskills: [shared/a, shared/a]\n",
])
def test_no_unknown_capabilities_or_arbitrary_skill_paths(tmp_path, payload):
    profile, folder = _role(tmp_path)
    (folder / "capabilities.yaml").write_text(payload, encoding="utf-8")
    with pytest.raises((ValueError, ValidationError)):
        select_role_research(profile)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_selected_skill_is_local_scoped_and_hash_verified(tmp_path, newline):
    profile, folder = _role(tmp_path)
    shared = tmp_path / "skills" / "shared" / "research"
    shared.mkdir(parents=True)
    source = shared / "SKILL.md"
    text = "---\nname: research\ndescription: Check factual source material\n---\nValidate evidence.\n"
    text = text.replace("\n", newline)
    source.write_bytes(text.encode("utf-8"))
    (folder / "capabilities.yaml").write_text(
        "capabilities: [claude_research]\nskills: [shared/research]\n", encoding="utf-8")
    selected = select_role_research(profile)
    assert len(selected) == 1
    assert selected[0]["name"] == "research"
    assert selected[0]["text"] == text
    assert selected[0]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    other, folder2 = _role(tmp_path, "other")
    assert select_role_research(other) is None
    (folder2 / "capabilities.yaml").write_text("capabilities: [claude_research]\n",encoding="utf-8")
    assert select_role_research(other) == []


def test_skill_cannot_use_frontmatter_to_grant_tools(tmp_path):
    profile, folder = _role(tmp_path)
    folder_skill = folder / "skills" / "research"
    folder_skill.mkdir(parents=True)
    f = folder_skill / "SKILL.md"
    (folder / "capabilities.yaml").write_text(
        "capabilities: [claude_research]\nskills: [personal/research]\n", encoding="utf-8")
    f.write_text("---\nname: research\ndescription: R\nallowed-tools: Bash\n---\nUnsafe", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        select_role_research(profile)


def test_skill_cannot_follow_links(tmp_path):
    profile, folder = _role(tmp_path)
    folder_skill = folder / "skills" / "research"
    folder_skill.mkdir(parents=True)
    f = folder_skill / "SKILL.md"
    (folder / "capabilities.yaml").write_text(
        "capabilities: [claude_research]\nskills: [personal/research]\n", encoding="utf-8")
    try:
        f.symlink_to(folder / "persona.yaml")
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows session lacks symbolic-link privilege; Linux CI checks real links")
        raise
    with pytest.raises(ValueError, match="linked"):
        select_role_research(profile)
