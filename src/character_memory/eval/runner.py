from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from character_memory.domain.models import Event, EventType


class EvalRunner:
    """Small deterministic JSONL regression layer; judge-model scoring stays separate."""

    def __init__(self, runtime):
        self.runtime = runtime

    def run_jsonl(self, path):
        results = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            event = Event(
                character_id=case.get("character_id", "rin"),
                event_type=EventType(case.get("event_type", "USER_MESSAGE")),
                event_time=datetime.fromisoformat(case["event_time"]),
                content=case["content"],
            )
            out = self.runtime.handle(event)
            action = out.reaction.action
            allowed = case.get("allowed_actions")
            ok = not allowed or action.type.value in allowed
            msg = action.message or ""
            ok = ok and all(x in msg for x in case.get("message_contains", []))
            ok = ok and all(x not in msg for x in case.get("message_not_contains", []))
            results.append(
                {
                    "id": case["id"],
                    "pass": ok,
                    "action": action.type.value,
                    "message": msg,
                    "reason": action.reason,
                    "recalled_memory_ids": [m.id for m in out.recalled_memories],
                }
            )
        return results
