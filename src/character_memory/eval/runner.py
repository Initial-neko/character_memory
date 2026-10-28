from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from character_memory.domain.models import EXPRESSIVE_ACTIONS, Event, EventType, Memory


# Outcome categories are deliberately separate. A green contract check is not a
# quality score: a case that still needs semantic review is `unscored`, even
# when every observable assertion passed.
STATUS_PASSED = "passed"
STATUS_ENGINEERING = "engineering"
STATUS_ENVIRONMENT = "environment"
STATUS_UNSCORED = "unscored"
STATUS_SEMANTIC = "semantic"
STATUSES = (
    STATUS_PASSED,
    STATUS_ENGINEERING,
    STATUS_ENVIRONMENT,
    STATUS_UNSCORED,
    STATUS_SEMANTIC,
)
FAILURE_CLASSES = (
    STATUS_ENGINEERING,
    STATUS_ENVIRONMENT,
    STATUS_UNSCORED,
    STATUS_SEMANTIC,
)


class EvalEnvironmentMissing(RuntimeError):
    """The suite asked for a dependency the local environment does not provide."""


_ENVIRONMENT_EXCEPTIONS = {
    "ImportError",
    "ModuleNotFoundError",
    "FileNotFoundError",
    "ConnectionError",
    "ConnectionRefusedError",
    "TimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "TransportError",
    "ProviderUnavailable",
}
_ENVIRONMENT_MARKERS = (
    "api_key",
    "api key",
    "missing opencode",
    "not configured",
    "embedding unavailable",
    "model not found",
    "no such model",
    "connection refused",
    "failed to connect",
    "does not exist",
)


def classify_exception(exc: BaseException) -> str:
    """Classify an execution error without pretending it is a quality verdict.

    Environment problems (missing runtime/provider/model) are reported
    separately from engineering defects so a broken local setup cannot be
    mistaken for a product regression.
    """
    if isinstance(exc, EvalEnvironmentMissing):
        return STATUS_ENVIRONMENT
    name = type(exc).__name__
    if name in _ENVIRONMENT_EXCEPTIONS:
        return STATUS_ENVIRONMENT
    if name == "ProviderHTTPError":
        code = getattr(exc, "status_code", None)
        if code in {401, 403, 404, 408, 429, 500, 502, 503, 504}:
            return STATUS_ENVIRONMENT
    text = str(exc).lower()
    if any(marker in text for marker in _ENVIRONMENT_MARKERS):
        return STATUS_ENVIRONMENT
    return STATUS_ENGINEERING


class EvalCostMeter:
    """Thread-safe model-call meter for one eval run.

    It has the same ``record(item)`` shape as ``LlmUsageRecorder`` so the CLI
    can either use it alone (offline model) or tee the real recorder into it
    without changing the provider client.
    """

    def __init__(self, *, paid: bool = False):
        self.paid = bool(paid)
        self._items: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, item: dict[str, Any]) -> int | None:
        with self._lock:
            self._items.append(dict(item))
            return len(self._items)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = [dict(item) for item in self._items]
        requests = len(items)
        logical = {
            str(item.get("logical_call_id") or f"request-{index}")
            for index, item in enumerate(items)
        }
        input_tokens = sum(int(item.get("input_tokens") or 0) for item in items)
        output_tokens = sum(int(item.get("output_tokens") or 0) for item in items)
        total_tokens = sum(int(item.get("total_tokens") or 0) for item in items)
        token_known = sum(1 for item in items if item.get("total_tokens") is not None)
        return {
            "model_calls": requests,
            "logical_calls": len(logical),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "token_known_calls": token_known,
            "token_coverage": (round(token_known / requests, 4) if requests else None),
            "provider_ms": round(sum(float(item.get("duration_ms") or 0.0) for item in items), 1),
            "paid_calls": requests if self.paid else 0,
        }


class NullCostMeter:
    def snapshot(self) -> dict[str, Any]:
        return EvalCostMeter().snapshot()


def cost_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Counter delta for one case; token coverage is recomputed from the delta."""
    result = {}
    for key in (
        "model_calls",
        "logical_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "token_known_calls",
        "provider_ms",
        "paid_calls",
    ):
        result[key] = round(float(after.get(key) or 0) - float(before.get(key) or 0), 1)
    for key in ("model_calls", "logical_calls", "input_tokens", "output_tokens", "total_tokens", "token_known_calls", "paid_calls"):
        result[key] = int(result[key])
    calls = result["model_calls"]
    result["token_coverage"] = (
        round(result["token_known_calls"] / calls, 4) if calls else None
    )
    return result


class EvalRunner:
    """JSONL contract runner with explicit failure classes and cost records.

    Accepts either one runtime or a character_id -> runtime mapping. The runner
    checks observable contracts only; semantic review is supplied independently
    through a review artifact and is never synthesized here.
    """

    def __init__(self, runtime, *, meter=None, reviews: dict[str, Any] | None = None):
        self.runtime = runtime
        self.meter = meter
        self.reviews = reviews or {}

    def _runtime_for(self, character_id: str):
        if isinstance(self.runtime, dict):
            if character_id not in self.runtime:
                raise EvalEnvironmentMissing(
                    f"unknown eval character_id={character_id!r}"
                )
            return self.runtime[character_id]
        return self.runtime

    def _cost_snapshot(self) -> dict[str, Any]:
        meter = self.meter
        if meter is None:
            first = self.runtime
            if isinstance(first, dict):
                first = next(iter(first.values()), None)
            meter = getattr(getattr(first, "model", None), "usage_recorder", None)
        if meter is not None and hasattr(meter, "snapshot"):
            return meter.snapshot()
        return NullCostMeter().snapshot()

    @staticmethod
    def _seed_events(runtime, character_id, seeds):
        """Import explicit synthetic fixtures, without buying extra reactions."""
        if not isinstance(seeds, list):
            raise ValueError("seed_events must be a list")
        event_ids, memory_ids = [], []
        for seed in seeds:
            if not isinstance(seed, dict):
                raise ValueError("each seed_event must be an object")
            event = Event(
                character_id=character_id,
                event_type=EventType(seed.get("event_type", "USER_MESSAGE")),
                event_time=datetime.fromisoformat(seed["event_time"]),
                content=seed["content"],
                metadata={**seed.get("metadata", {}), "eval_fixture": True},
            )
            # Embed before any write/transaction. A failed embedding leaves no
            # partial source fixture and cannot hold SQLite's write lock.
            specs = seed.get("memories", [])
            if not isinstance(specs, list) or any(not isinstance(spec, dict) for spec in specs):
                raise ValueError("seed memories must be a list of objects")
            memories = [Memory(
                character_id=character_id, content=spec["content"],
                memory_type=spec.get("memory_type", "EPISODIC"),
                event_time=event.event_time, importance=spec.get("importance", .5),
                embedding=runtime.embeddings.embed(spec["content"]),
                metadata={"eval_fixture": True},
            ) for spec in specs]
            with runtime.store.transaction():
                # Eval fixtures may seed any event type; route chat turns through the
                # one conversation writer so they behave like real history.
                source = runtime.store.append_fact(event)
                ids = [runtime.store.add_memory(memory.model_copy(update={"source_event_id": source.id})).id
                       for memory in memories]
            event_ids.append(source.id)
            memory_ids.extend(ids)
        return event_ids, memory_ids

    @staticmethod
    def _review_verdict(raw) -> tuple[str | None, dict[str, Any]]:
        if raw is None:
            return None, {}
        if isinstance(raw, str):
            raw = {"verdict": raw}
        if not isinstance(raw, dict):
            raise ValueError("review entry must be an object")
        verdict = str(raw.get("verdict") or "").strip().lower()
        verdict = {"pass": STATUS_PASSED, "fail": STATUS_SEMANTIC}.get(verdict, verdict)
        if verdict not in {STATUS_PASSED, STATUS_SEMANTIC, STATUS_UNSCORED}:
            raise ValueError(
                "review verdict must be passed, semantic or unscored"
            )
        return verdict, dict(raw)

    def _review_for(self, case_id: str, required: bool) -> dict[str, Any] | None:
        if not required:
            return None
        raw = self.reviews.get(case_id)
        if raw is None:
            return {
                "required": True,
                "verdict": None,
                "reviewer": None,
                "notes": "",
                "state": "missing",
            }
        verdict, entry = self._review_verdict(raw)
        return {
            "required": True,
            "verdict": verdict,
            "reviewer": entry.get("reviewer"),
            "notes": str(entry.get("notes") or ""),
            "reviewed_at": entry.get("reviewed_at"),
            "state": "reviewed",
        }

    @staticmethod
    def _resolve_status(contract_passed: bool, review: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
        if not contract_passed:
            return STATUS_ENGINEERING, None
        if review is None:
            return STATUS_PASSED, None
        verdict = review.get("verdict")
        if verdict == STATUS_PASSED:
            return STATUS_PASSED, None
        if verdict == STATUS_SEMANTIC:
            return STATUS_SEMANTIC, {
                "class": STATUS_SEMANTIC,
                "reason": "independent semantic review failed",
                "review_notes": review.get("notes") or "",
            }
        return STATUS_UNSCORED, {
            "class": STATUS_UNSCORED,
            "reason": "semantic review required but not performed",
        }

    def run_jsonl(self, path):
        results = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            results.append(self.run_case(case))
        return results

    def run_case(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            raise ValueError("each eval case needs an id")
        character_id = case.get("character_id", "rin")
        criteria = case.get("review_criteria", [])
        if not isinstance(criteria, list) or any(not isinstance(item, str) for item in criteria):
            raise ValueError("review_criteria must be a list of strings")
        if "messages_equal" in case:
            expected = case["messages_equal"]
            if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
                raise ValueError("messages_equal must be a list of strings")

        event = Event(
            character_id=character_id,
            event_type=EventType(case.get("event_type", "USER_MESSAGE")),
            event_time=datetime.fromisoformat(case["event_time"]),
            content=case["content"],
            metadata={"conversation_id": case.get("conversation_id", f"eval:{character_id}")},
        )
        review_required = bool(criteria)
        before = self._cost_snapshot()
        started = time.perf_counter()
        result: dict[str, Any]
        try:
            runtime = self._runtime_for(character_id)
            seed_ids, seed_memory_ids = self._seed_events(runtime, character_id, case.get("seed_events", []))
            out = runtime.handle(event)
            result = self._success_result(case, case_id, character_id, criteria, out, runtime)
            if "seed_events" in case:
                result.update(seed_event_ids=seed_ids, seed_memory_ids=seed_memory_ids)
        except Exception as exc:  # noqa: BLE001 - classified, never silently green
            failure_class = classify_exception(exc)
            result = {
                "id": case_id,
                "domain": case.get("domain"),
                "tags": case.get("tags", []),
                "character_id": character_id,
                "status": failure_class,
                "pass": False,
                "contract_passed": False,
                "checks": [],
                "actions": [],
                "messages": [],
                "message_count": 0,
                "silent": True,
                "perception_present": False,
                "reaction_present": False,
                "perception": "",
                "reaction": "",
                "recalled_memory_ids": [],
                "created_memory_ids": [],
                "memory_decisions": [],
                "review_required": review_required,
                "review_criteria": criteria,
                "review": None,
                "failure": {
                    "class": failure_class,
                    "reason": "execution error",
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:600],
                },
            }
        finally:
            wall_ms = round((time.perf_counter() - started) * 1000, 1)
        after = self._cost_snapshot()
        result["cost"] = cost_delta(before, after)
        result["duration_ms"] = wall_ms
        return result

    def _success_result(self, case, case_id, character_id, criteria, out, runtime) -> dict[str, Any]:
        actions = list(out.reaction.actions)
        action_types = [action.type.value for action in actions]
        observed_actions = action_types or ["NO_REPLY"]
        messages = [action.message or "" for action in actions if action.message]
        joined = "\n".join(messages)
        visible_actions = [action for action in actions if action.type in EXPRESSIVE_ACTIONS]
        silent = not visible_actions

        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, detail: str = "") -> None:
            checks.append({"name": name, "passed": bool(passed), "detail": detail})

        allowed = case.get("allowed_actions")
        if allowed:
            unexpected = [action for action in observed_actions if action not in allowed]
            check("allowed_actions", not unexpected, f"unexpected={unexpected}")
        if "expect_silence" in case:
            check(
                "expect_silence",
                silent is bool(case["expect_silence"]),
                f"silent={silent} expected={bool(case['expect_silence'])}",
            )
        if "min_messages" in case:
            check("min_messages", len(messages) >= int(case["min_messages"]), f"messages={len(messages)}")
        if "max_messages" in case:
            check("max_messages", len(messages) <= int(case["max_messages"]), f"messages={len(messages)}")
        for text in case.get("message_contains", []):
            check("message_contains", text in joined, f"text={text!r} found={text in joined}")
        for text in case.get("message_not_contains", []):
            check("message_not_contains", text not in joined, f"forbidden={text!r} found={text in joined}")
        if "messages_equal" in case:
            check("messages_equal", messages == case["messages_equal"], "exact message sequence")
        if case.get("require_safe_summary"):
            check(
                "require_safe_summary",
                bool(out.reaction.perception.strip()) and bool(out.reaction.reaction.strip()),
                "perception and reaction must both be present",
            )
        if "min_memory_writes" in case:
            check("min_memory_writes", len(out.created_memory_ids) >= int(case["min_memory_writes"]), f"writes={len(out.created_memory_ids)}")
        if "max_memory_writes" in case:
            check("max_memory_writes", len(out.created_memory_ids) <= int(case["max_memory_writes"]), f"writes={len(out.created_memory_ids)}")
        if "min_recalled_memories" in case:
            check("min_recalled_memories", len(out.recalled_memories) >= int(case["min_recalled_memories"]), f"recalled={len(out.recalled_memories)}")
        for text in case.get("context_contains", []):
            check("context_contains", text in out.context, f"text={text!r} found={text in out.context}")

        contract_passed = all(item["passed"] for item in checks)
        trace = runtime.store.get_runtime_trace(out.event.id)
        review = self._review_for(case_id, bool(criteria))
        status, failure = self._resolve_status(contract_passed, review)
        if not contract_passed:
            failed = [item["name"] for item in checks if not item["passed"]]
            failure = {
                "class": STATUS_ENGINEERING,
                "reason": "contract checks failed",
                "failed_checks": failed,
            }

        return {
            "id": case_id,
            "domain": case.get("domain"),
            "tags": case.get("tags", []),
            "character_id": character_id,
            "status": status,
            "pass": status == STATUS_PASSED,
            "contract_passed": contract_passed,
            "checks": checks,
            "actions": action_types,
            "messages": messages,
            "message_count": len(messages),
            "silent": silent,
            "perception_present": bool(out.reaction.perception.strip()),
            "reaction_present": bool(out.reaction.reaction.strip()),
            "perception": out.reaction.perception,
            "reaction": out.reaction.reaction,
            "recalled_memory_ids": [memory.id for memory in out.recalled_memories],
            "created_memory_ids": out.created_memory_ids,
            "memory_decisions": (trace or {}).get("memory_decisions", []),
            "review_required": bool(criteria),
            "review_criteria": criteria,
            "review": review,
            "failure": failure,
        }
