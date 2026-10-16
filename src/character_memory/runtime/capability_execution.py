"""A bounded execution contract for already-chosen World requests.

No character decision, prompt, scheduler or provider retry loop lives here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import math
import re

from httpx import TimeoutException

from character_memory.domain.models import WorldObservation


@dataclass(frozen=True)
class CapabilityRequest:
    request_id: str
    character_id: str
    opportunity_id: str
    source_event_id: int | None
    channel: str
    capability: str
    arguments: dict
    constraints: dict


@dataclass(frozen=True)
class CapabilityResult:
    request_id: str
    status: str
    data: dict = field(default_factory=dict)
    reason: str = ""


def adapt_browse_decision(plan, *, character_id: str, opportunity_id: str,
                          source_event_id: int | None, max_pages: int = 2,
                          max_chars_per_page: int = 6000) -> CapabilityRequest | None:
    if not plan.browse:
        return None
    return CapabilityRequest(
        request_id=f"{opportunity_id}:WEB_SEARCH:1", character_id=character_id,
        opportunity_id=opportunity_id, source_event_id=source_event_id,
        channel="WORLD", capability="WEB_SEARCH", arguments={"query": plan.query},
        constraints={"max_pages": max_pages, "max_chars_per_page": max_chars_per_page, "max_calls": 1},
    )


class CapabilityExecutor:
    """Authorize deterministically and reuse existing Search/Browser defenses.

    The composition caller owns trusted identity, opportunity and constraints.
    They must not be copied from an LLM or external document. Provider-specific
    timeouts/retries remain owned by the existing observer's dependencies.
    """
    MIGRATION = "world/004-capability-executions"

    def __init__(self, store, web_observer, *, rss_reader=None, claude_runner=None):
        self.store = store
        self.web_observer = web_observer
        self.rss_reader = rss_reader
        self.claude_runner = claude_runner
        self.store.apply_schema_migration(self.MIGRATION, self._create_schema, immediate=True)
        self.store.apply_schema_migration("world/005-execution-metadata", self._add_metadata, immediate=True)

    def _add_metadata(self):
        if "execution_metadata_json" not in {row[1] for row in self.store.conn.execute("PRAGMA table_info(capability_executions)")}:
            self.store.conn.execute("ALTER TABLE capability_executions ADD COLUMN execution_metadata_json TEXT NOT NULL DEFAULT '{}'")

    def _create_schema(self):
        self.store.conn.execute(
            "CREATE TABLE IF NOT EXISTS capability_executions("
            "request_id TEXT PRIMARY KEY,character_id TEXT NOT NULL,opportunity_id TEXT NOT NULL,"
            "request_json TEXT NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,"
            "completed_at TEXT,result_json TEXT)"
        )
        self.store.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_capability_opportunity "
            "ON capability_executions(character_id,opportunity_id,status)"
        )

    @staticmethod
    def _denial(request: CapabilityRequest) -> str:
        task = request.channel == "DIRECT" and request.capability == "CLAUDE_TASK"
        if request.channel != "WORLD" and not task:
            return "CHANNEL_DENIED"
        if request.capability not in {"WEB_SEARCH", "READ_RSS"} and not task:
            return "CAPABILITY_DENIED"
        if any(not isinstance(value, str) or not value.strip() or len(value) > 256
               for value in (request.request_id, request.character_id, request.opportunity_id)):
            return "INVALID_IDENTITY"
        if request.source_event_id is not None and (type(request.source_event_id) is not int or request.source_event_id < 1):
            return "INVALID_IDENTITY"
        if not isinstance(request.arguments, dict):
            return "INVALID_ARGUMENTS"
        if request.capability == "READ_RSS":
            items = request.arguments.get("items")
            if (set(request.arguments) != {"items"} or not isinstance(items, list)
                    or not 1 <= len(items) <= 2
                    or any(not isinstance(item, dict) or set(item) != {"item_id", "source_generation"}
                           or type(item["item_id"]) is not int or item["item_id"] < 1
                           or type(item["source_generation"]) is not int or item["source_generation"] < 0
                           for item in items)
                    or len({item["item_id"] for item in items}) != len(items)):
                return "INVALID_ARGUMENTS"
            bounds = request.constraints
            if (not isinstance(bounds, dict) or set(bounds) != {"max_items", "max_chars", "max_calls"}
                    or any(type(value) is not int for value in bounds.values())
                    or not 1 <= bounds["max_items"] <= 2 or len(items) > bounds["max_items"]
                    or not 500 <= bounds["max_chars"] <= 12000 or not 0 <= bounds["max_calls"] <= 1):
                return "INVALID_CONSTRAINTS"
            return "BUDGET_EXHAUSTED" if bounds["max_calls"] == 0 else ""
        query = request.arguments.get("query")
        if set(request.arguments) != {"query"} or not isinstance(query, str) or not query.strip() or len(query) > 240:
            return "INVALID_ARGUMENTS"
        bounds = request.constraints
        if not isinstance(bounds, dict):
            return "INVALID_CONSTRAINTS"
        base = {"max_pages", "max_chars_per_page", "max_calls"}
        policy = {"backend", "claude_daily_max", "claude_daily_budget_usd", "claude_max_budget_usd", "cost_saving"}
        optional = {"claude_timeout_seconds", "claude_max_turns", "claude_max_tool_calls", "claude_model", "role_grant", "claude_skills"}
        if bounds.get("backend") == "claude":
            if bounds.get("role_grant") != ("claude_task" if task else "claude_research"):
                return "ROLE_CAPABILITY_DENIED"
            if not base | policy <= set(bounds) or set(bounds) - base - policy - optional:
                return "INVALID_CONSTRAINTS"
            skills = bounds.get("claude_skills", [])
            if (not isinstance(skills, list) or len(skills) > 4
                    or any(not isinstance(item, dict) or set(item) != {"name", "text", "sha256"}
                           or not isinstance(item["name"], str) or not isinstance(item["text"], str)
                           or not isinstance(item["sha256"], str)
                           or len(item["text"].encode("utf-8")) > 8192
                           or re.fullmatch(r"[a-z][a-z0-9-]{0,47}", item["name"]) is None
                           or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
                           or hashlib.sha256(item["text"].encode("utf-8")).hexdigest() != item["sha256"]
                           for item in skills)
                    or len({item["name"] for item in skills}) != len(skills)):
                return "INVALID_CONSTRAINTS"
            if type(bounds["claude_daily_max"]) is not int or not 0 <= bounds["claude_daily_max"] <= 200 or type(bounds["cost_saving"]) is not bool:
                return "INVALID_CONSTRAINTS"
            for key in ("claude_daily_budget_usd", "claude_max_budget_usd"):
                value = bounds[key]
                if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value <= 0):
                    return "INVALID_CONSTRAINTS"
            if bounds["cost_saving"] and bounds["claude_daily_budget_usd"] is not None and bounds["claude_max_budget_usd"] is None:
                return "INVALID_CONSTRAINTS"
            for key, low, high in (("claude_timeout_seconds", 1, 600), ("claude_max_turns", 1, 20), ("claude_max_tool_calls", 1, 4)):
                types = {int, float} if key == "claude_timeout_seconds" else {int}
                if key in bounds and (type(bounds[key]) not in types or not low <= bounds[key] <= high):
                    return "INVALID_CONSTRAINTS"
            if "claude_model" in bounds and (not isinstance(bounds["claude_model"], str) or len(bounds["claude_model"]) > 120):
                return "INVALID_CONSTRAINTS"
        elif task or set(bounds) != base:
            return "INVALID_CONSTRAINTS"
        if (any(type(bounds[key]) is not int for key in base)
                or not 1 <= bounds["max_pages"] <= 4
                or not 500 <= bounds["max_chars_per_page"] <= 16000
                or not 0 <= bounds["max_calls"] <= 1):
            return "INVALID_CONSTRAINTS"
        if bounds["max_calls"] == 0:
            return "BUDGET_EXHAUSTED"
        return ""

    def has_completed_receipt(self, request_id: str, *, character_id: str, opportunity_id: str) -> bool:
        """Read a scoped cache hint; execute still verifies the complete payload."""
        return self.store.conn.execute(
            "SELECT 1 FROM capability_executions WHERE request_id=? AND character_id=? "
            "AND opportunity_id=? AND result_json IS NOT NULL",
            (request_id, character_id, opportunity_id),
        ).fetchone() is not None

    def claude_budget_denial(self, character_id: str, *, now: datetime,
                             daily_max: int, daily_budget_usd: float | None,
                             max_budget_usd: float | None, cost_saving: bool) -> str:
        """Read current durable allowance without reserving it.

        Callers supply trusted server limits. Discovery is only a snapshot;
        execute repeats this same check inside its reservation transaction.
        """
        day = now.astimezone().date().isoformat()
        entries = self.store.conn.execute(
            "SELECT execution_metadata_json FROM capability_executions WHERE character_id=? "
            "AND json_extract(execution_metadata_json,'$.backend')='claude' "
            "AND json_extract(execution_metadata_json,'$.day')=?", (character_id, day)).fetchall()
        if len(entries) >= daily_max:
            return "CLAUDE_DAILY_LIMIT"
        if cost_saving and daily_budget_usd is not None:
            # Missing a per-call reservation cannot prove enough budget remains.
            if max_budget_usd is None:
                return "INVALID_CONSTRAINTS"
            spend = 0.0
            for entry in entries:
                previous = json.loads(entry[0])
                actual = previous.get("estimated_cost_usd")
                reservation = previous.get("reservation_usd") or 0.0
                spend += actual if previous.get("usage_state") == "COMPLETE" and actual is not None else max(actual or 0.0, reservation)
            if spend + max_budget_usd > daily_budget_usd:
                return "CLAUDE_DAILY_BUDGET"
        return ""

    def execute(self, request: CapabilityRequest, *, now: datetime | None = None) -> CapabilityResult:
        denial = self._denial(request)
        if denial:
            return CapabilityResult(request.request_id, "DENIED", reason=denial)
        now = now or datetime.now().astimezone()
        payload = asdict(request)
        is_claude = request.capability in {"WEB_SEARCH", "CLAUDE_TASK"} and request.constraints.get("backend") == "claude"
        metadata = {}
        # Reserve before any external call; failed/time-out attempts consume the
        # opportunity's finite allowance just like successful attempts.
        with self.store.transaction(immediate=True):
            row = self.store.conn.execute(
                "SELECT * FROM capability_executions WHERE request_id=?", (request.request_id,)
            ).fetchone()
            if row is not None:
                if json.loads(row["request_json"]) != payload:
                    return CapabilityResult(request.request_id, "DENIED", reason="REQUEST_ID_CONFLICT")
                if row["result_json"] is not None:
                    return CapabilityResult(**json.loads(row["result_json"]))
                return CapabilityResult(request.request_id, "SKIPPED", reason="IN_PROGRESS_OR_INTERRUPTED")
            used = self.store.conn.execute(
                "SELECT COUNT(*) FROM capability_executions WHERE character_id=? AND opportunity_id=? "
                "AND status IN ('STARTED','SUCCESS','FAILED','TIMEOUT')",
                (request.character_id, request.opportunity_id),
            ).fetchone()[0]
            if used >= request.constraints["max_calls"]:
                return CapabilityResult(request.request_id, "DENIED", reason="BUDGET_EXHAUSTED")
            if is_claude:
                if self.claude_runner is None:
                    return CapabilityResult(request.request_id, "DENIED", reason="CLAUDE_UNAVAILABLE")
                day = now.astimezone().date().isoformat()
                denial = self.claude_budget_denial(
                    request.character_id, now=now,
                    daily_max=request.constraints["claude_daily_max"],
                    daily_budget_usd=request.constraints["claude_daily_budget_usd"],
                    max_budget_usd=request.constraints["claude_max_budget_usd"],
                    cost_saving=request.constraints["cost_saving"],
                )
                if denial:
                    return CapabilityResult(request.request_id, "DENIED", reason=denial)
                reserved = request.constraints["claude_max_budget_usd"]
                metadata = {"backend": "claude", "day": day, "reservation_usd": reserved, "usage_state": "UNKNOWN"}
            self.store.conn.execute(
                "INSERT INTO capability_executions(request_id,character_id,opportunity_id,request_json,status,started_at,execution_metadata_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (request.request_id, request.character_id, request.opportunity_id,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), "STARTED", now.isoformat(), json.dumps(metadata)),
            )
        try:
            if is_claude:
                result = self.claude_runner.run(request)
                metadata.update(result.data.get("execution") or {})
            elif request.capability == "READ_RSS":
                if self.rss_reader is None:
                    raise RuntimeError("RSS_READER_UNAVAILABLE")
                data = self.rss_reader.read(
                    request.character_id, request.request_id, request.arguments["items"],
                    max_items=request.constraints["max_items"], max_chars=request.constraints["max_chars"], now=now,
                )
                result = CapabilityResult(request.request_id, "SUCCESS" if data["items"] else "FAILED", data,
                                          "" if data["items"] else "NO_READABLE_CONTENT")
            else:
                observed = self.web_observer.observe(
                    request.arguments["query"], max_pages=request.constraints["max_pages"],
                    max_chars_per_page=request.constraints["max_chars_per_page"],
                )
                pages = []
                for item in (observed.get("observations") or [])[:request.constraints["max_pages"]]:
                    page = WorldObservation.model_validate(item)
                    page = page.model_copy(update={"content": page.content[:request.constraints["max_chars_per_page"]]})
                    pages.append(page.model_dump(mode="json"))
                data = {"observations": pages, "search_results": int(observed.get("search_results") or 0),
                        "errors": list(observed.get("errors") or [])}
                result = CapabilityResult(request.request_id, "SUCCESS" if pages else "FAILED", data,
                                          "" if pages else "NO_READABLE_CONTENT")
        except (TimeoutError, TimeoutException) as error:
            result = CapabilityResult(request.request_id, "TIMEOUT", reason=str(error)[:500])
        except Exception as error:
            result = CapabilityResult(request.request_id, "FAILED", reason="CLAUDE_EXECUTION_FAILED" if is_claude else str(error)[:500])
        # If this local write fails, STARTED remains unknown. Do not rerun the
        # provider to make up for a missing local receipt.
        with self.store.transaction(immediate=True):
            self.store.conn.execute(
                "UPDATE capability_executions SET status=?,completed_at=?,result_json=?,execution_metadata_json=? WHERE request_id=? AND status='STARTED'",
                (result.status, datetime.now().astimezone().isoformat(),
                 json.dumps(asdict(result), ensure_ascii=False), json.dumps(metadata, ensure_ascii=False), request.request_id),
            )
        return result
