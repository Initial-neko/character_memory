from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from character_memory.domain.models import ActionType, Event, EventType


_VISIBLE_ACTIONS = {
    ActionType.REPLY,
    ActionType.MINIMAL_RESPONSE,
    ActionType.PROACTIVE_MESSAGE,
    ActionType.MESSAGE,
    ActionType.EMOJI,
    ActionType.STICKER,
    ActionType.IMAGE,
}


class EvalRunner:
    """Small JSONL regression layer; judge-model scoring stays separate.

    Accepts either one runtime or a character_id -> runtime mapping. The runner
    checks observable contracts only; it never scores hidden reasoning.
    """

    def __init__(self, runtime):
        self.runtime = runtime

    def _runtime_for(self, character_id: str):
        if isinstance(self.runtime, dict):
            if character_id not in self.runtime:
                raise KeyError(f"unknown eval character_id={character_id!r}")
            return self.runtime[character_id]
        return self.runtime

    def run_jsonl(self, path):
        results = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            character_id = case.get("character_id", "rin")
            event = Event(
                character_id=character_id,
                event_type=EventType(case.get("event_type", "USER_MESSAGE")),
                event_time=datetime.fromisoformat(case["event_time"]),
                content=case["content"],
                metadata={"conversation_id": case.get("conversation_id", f"eval:{character_id}")},
            )
            runtime = self._runtime_for(character_id)
            out = runtime.handle(event)
            actions = list(out.reaction.actions)
            action_types = [action.type.value for action in actions]
            observed_actions = action_types or ["NO_REPLY"]
            messages = [action.message or "" for action in actions if action.message]
            joined = "\n".join(messages)
            visible_actions = [action for action in actions if action.type in _VISIBLE_ACTIONS]
            silent = not visible_actions

            ok = True
            allowed = case.get("allowed_actions")
            if allowed:
                ok = ok and all(action in allowed for action in observed_actions)
            if "expect_silence" in case:
                ok = ok and silent is bool(case["expect_silence"])
            if "min_messages" in case:
                ok = ok and len(messages) >= int(case["min_messages"])
            if "max_messages" in case:
                ok = ok and len(messages) <= int(case["max_messages"])
            ok = ok and all(text in joined for text in case.get("message_contains", []))
            ok = ok and all(text not in joined for text in case.get("message_not_contains", []))
            if case.get("require_safe_summary"):
                ok = ok and bool(out.reaction.perception.strip()) and bool(out.reaction.reaction.strip())
            if "min_memory_writes" in case:
                ok = ok and len(out.created_memory_ids) >= int(case["min_memory_writes"])
            if "max_memory_writes" in case:
                ok = ok and len(out.created_memory_ids) <= int(case["max_memory_writes"])
            if "min_recalled_memories" in case:
                ok = ok and len(out.recalled_memories) >= int(case["min_recalled_memories"])
            ok = ok and all(text in out.context for text in case.get("context_contains", []))

            trace = runtime.store.get_runtime_trace(out.event.id)
            results.append(
                {
                    "id": case["id"],
                    "tags": case.get("tags", []),
                    "character_id": character_id,
                    "pass": ok,
                    "actions": action_types,
                    "messages": messages,
                    "message_count": len(messages),
                    "silent": silent,
                    "perception_present": bool(out.reaction.perception.strip()),
                    "reaction_present": bool(out.reaction.reaction.strip()),
                    "recalled_memory_ids": [memory.id for memory in out.recalled_memories],
                    "created_memory_ids": out.created_memory_ids,
                    "memory_decisions": (trace or {}).get("memory_decisions", []),
                }
            )
        return results
