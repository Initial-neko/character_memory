"""Operator-owned, fail-closed capability and Skill selection for each person.

No personality text, LLM output, or Vocation field can grant execution rights.
Only the named character's sidecar may opt into Claude research.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml


SKILL_ID = re.compile(r"^(shared|personal)/([a-z][a-z0-9-]{0,47})$")
MAX_SKILL_BYTES = 8192
MAX_CONFIG_BYTES = 4096


class CharacterCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capabilities: list[str] = Field(default_factory=list, max_length=2)
    skills: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def check_allowlist(self):
        if len(set(self.capabilities)) != len(self.capabilities) or set(self.capabilities) - {"claude_research", "claude_task"}:
            raise ValueError("unknown or duplicate character capability")
        if len(set(self.skills)) != len(self.skills):
            raise ValueError("duplicate selected Skills")
        if any(SKILL_ID.fullmatch(item) is None for item in self.skills):
            raise ValueError("Skill must be shared/<slug> or personal/<slug>")
        if self.skills and not {"claude_research", "claude_task"}.intersection(self.capabilities):
            raise ValueError("Skills require explicit claude_research permission")
        return self


def _regular_bounded_file(path: Path, maximum: int) -> bytes:
    # Reject symlinks even if they resolve inside an otherwise trusted folder:
    # an operator config must never become a pointer to private files.
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing or linked capability file")
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("capability file too large")
    return raw


def select_role_research(profile: dict | None, *, capability: str = "claude_research") -> list[dict] | None:
    """Return selected immutable Skill snapshots; None means no Claude grant.

    Missing or invalid sidecars fail closed. An authorized role with no Skills
    returns [], so research still works with the original World MCP tool.
    """
    if not profile or not profile.get("persona_path"):
        return None
    persona = Path(profile["persona_path"])
    if "archived_at" in profile or not persona.is_file():
        return None
    settings_path = persona.parent / "capabilities.yaml"
    if not settings_path.exists():
        return None
    payload = yaml.safe_load(_regular_bounded_file(settings_path, MAX_CONFIG_BYTES))
    if not isinstance(payload, dict):
        raise ValueError("capabilities.yaml must be a mapping")
    permissions = CharacterCapabilities.model_validate(payload)
    if capability not in {"claude_research", "claude_task"} or capability not in permissions.capabilities:
        return None
    picked: list[dict] = []
    for identity in permissions.skills:
        kind, slug = SKILL_ID.fullmatch(identity).groups()
        folder = persona.parent / "skills" / slug if kind == "personal" else persona.parent.parent.parent / "skills" / "shared" / slug
        source = folder / "SKILL.md"
        if folder.is_symlink() or source.is_symlink():
            raise ValueError("linked Skills are not allowed")
        # V1 supports only reviewed, pure-text Skills; no scripts, supporting
        # files or arbitrary filesystem grants are copied into Claude's cwd.
        raw = _regular_bounded_file(source, MAX_SKILL_BYTES)
        content = raw.decode("utf-8")
        header_text = content.replace("\r\n", "\n")
        if not header_text.startswith("---\n") or "\n---\n" not in header_text[4:]:
            raise ValueError("Skill requires YAML frontmatter")
        header = header_text.split("\n---\n", 1)[0][4:]
        metadata = yaml.safe_load(header)
        if not isinstance(metadata, dict) or set(metadata) != {"name", "description"} or metadata.get("name") != slug or not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
            raise ValueError("Skill frontmatter must contain matching name and description only")
        picked.append({"name": slug, "text": content, "sha256": hashlib.sha256(raw).hexdigest()})
    if len({item["name"] for item in picked}) != len(picked):
        raise ValueError("Skill names must be unique across shared and personal scope")
    return picked
