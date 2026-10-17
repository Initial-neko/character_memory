"""Read-only descriptions of existing execution capabilities.

Availability is a trusted, per-turn server snapshot, never model input or a
permission token. Discovery does not reserve budget, execute tools, initialize
providers or persist anything. Dispatch must still recheck current permission
and use CapabilityExecutor's validation, budget and durable receipts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import yaml
import re

from character_memory.character_capabilities import select_role_research, read_role_capabilities


@dataclass(frozen=True)
class CapabilityAvailability:
    web_search: bool = False
    rss_reading: bool = False
    claude: bool = False
    direct_task: bool = False
    world_backend: Literal["direct", "claude"] = "direct"
    remaining_calls: int = 1

    def __post_init__(self):
        if any(type(value) is not bool for value in
               (self.web_search, self.rss_reading, self.claude, self.direct_task)):
            raise ValueError("availability flags must be server booleans")
        if self.world_backend not in {"direct", "claude"}:
            raise ValueError("unknown World execution backend")
        if type(self.remaining_calls) is not int or self.remaining_calls < 0:
            raise ValueError("remaining_calls must be a nonnegative integer")


@dataclass(frozen=True)
class CapabilityDescription:
    id: str
    summary: str
    arguments_schema: dict
    channel: str
    backend: str
    skills: tuple[dict, ...] = ()
    required_grant: str | None = None


def _query_schema() -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 240, "pattern": r"\S"}},
            "required": ["query"]}


def _rss_schema() -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": {"items": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True,
                "description": "Select current eligible items; each item_id must occur only once, even across source generations.",
                "items": {"type": "object", "additionalProperties": False,
                    "properties": {"item_id": {"type": "integer", "minimum": 1},
                                   "source_generation": {"type": "integer", "minimum": 0}},
                    "required": ["item_id", "source_generation"]}}},
            "required": ["items"]}


def _skills(profile: dict, grant: str) -> tuple[dict, ...] | None:
    try:
        selected = select_role_research(profile, capability=grant)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return None
    return None if selected is None else tuple(selected)


def runner_available(runner) -> bool:
    """Read an existing runner adapter's optional local readiness probe."""
    if not callable(getattr(runner, "run", None)):
        return False
    probe = getattr(runner, "is_available", None)
    try:
        return probe() is True if callable(probe) else True
    except OSError:
        return False


def available_capabilities(
    profile: dict | None, *, channel: str, availability: CapabilityAvailability,
    extensions: tuple[CapabilityDescription, ...] = (),
) -> tuple[CapabilityDescription, ...]:
    """Describe existing tools for one server-selected active person/channel.

    ``direct_task`` means the caller already checked the Direct USER_MESSAGE
    event/configuration boundary. Reader flags reflect configured dependencies
    and eligible RSS candidates; ``remaining_calls`` is the caller's current
    opportunity allowance. These are availability hints, not execution grants.
    Missing Claude grants preserve World's existing direct-search fallback.
    Neither Browser sessions nor external writes are registered by this API.
    """
    if (not isinstance(channel, str) or channel not in {"WORLD", "DIRECT", "GROUP", "SPACE", "VISUAL"}
            or not isinstance(profile, dict) or "archived_at" in profile
            or not isinstance(profile.get("id"), str)
            or not profile["id"].strip() or len(profile["id"]) > 256
            or availability.remaining_calls == 0):
        return ()
    extra = []
    if extensions:
        try:
            permissions = read_role_capabilities(profile)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            permissions = None
        seen = {"WEB_SEARCH", "READ_RSS", "CLAUDE_TASK"}
        for item in extensions:
            if (isinstance(item, CapabilityDescription) and isinstance(item.id, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item.id) is not None
                    and item.id not in seen and item.channel == channel
                    and item.required_grant is not None and permissions is not None
                    and item.required_grant in permissions.capabilities):
                extra.append(item)
                seen.add(item.id)
    if channel == "DIRECT":
        if not (availability.direct_task and availability.claude):
            return tuple(extra)
        skills = _skills(profile, "claude_task")
        if skills is None:
            return tuple(extra)
        return (CapabilityDescription(
            "CLAUDE_TASK", "Ask the isolated executor a self-contained public question; its response is not personal experience.",
            _query_schema(), channel, "claude", skills), *extra)
    if channel != "WORLD":
        return tuple(extra)
    tools = []
    if availability.web_search:
        backend = "direct"
        skills = None
        if availability.world_backend == "claude":
            skills = _skills(profile, "claude_research")
            if skills is not None:
                backend = "claude"
        if backend == "direct" or availability.claude:
            tools.append(CapabilityDescription(
                "WEB_SEARCH", "Find and read bounded public sources; only returned source receipts establish what was read.",
                _query_schema(), channel, backend, skills or ()))
    if availability.rss_reading:
        tools.append(CapabilityDescription(
            "READ_RSS", "Read selected local Feed text; this does not fetch or establish reading of the linked original page.",
            _rss_schema(), channel, "local"))
    return tuple(tools + extra)
