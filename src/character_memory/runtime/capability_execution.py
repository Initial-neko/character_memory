"""A bounded execution contract for already-chosen World requests.

No character decision, prompt, scheduler or provider retry loop lives here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json

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

    def __init__(self, store, web_observer, *, rss_reader=None):
        self.store = store
        self.web_observer = web_observer
        self.rss_reader = rss_reader
        self.store.apply_schema_migration(self.MIGRATION, self._create_schema, immediate=True)

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
        if request.channel != "WORLD":
            return "CHANNEL_DENIED"
        if request.capability not in {"WEB_SEARCH", "READ_RSS"}:
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
        if (set(bounds) != {"max_pages", "max_chars_per_page", "max_calls"}
                or any(type(value) is not int for value in bounds.values())
                or not 1 <= bounds["max_pages"] <= 4
                or not 500 <= bounds["max_chars_per_page"] <= 16000
                or not 0 <= bounds["max_calls"] <= 1):
            return "INVALID_CONSTRAINTS"
        if bounds["max_calls"] == 0:
            return "BUDGET_EXHAUSTED"
        return ""

    def execute(self, request: CapabilityRequest, *, now: datetime | None = None) -> CapabilityResult:
        denial = self._denial(request)
        if denial:
            return CapabilityResult(request.request_id, "DENIED", reason=denial)
        now = now or datetime.now().astimezone()
        payload = asdict(request)
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
            self.store.conn.execute(
                "INSERT INTO capability_executions(request_id,character_id,opportunity_id,request_json,status,started_at) "
                "VALUES(?,?,?,?,?,?)",
                (request.request_id, request.character_id, request.opportunity_id,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), "STARTED", now.isoformat()),
            )
        try:
            if request.capability == "READ_RSS":
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
            result = CapabilityResult(request.request_id, "FAILED", reason=str(error)[:500])
        # If this local write fails, STARTED remains unknown. Do not rerun the
        # provider to make up for a missing local receipt.
        with self.store.transaction(immediate=True):
            self.store.conn.execute(
                "UPDATE capability_executions SET status=?,completed_at=?,result_json=? WHERE request_id=? AND status='STARTED'",
                (result.status, datetime.now().astimezone().isoformat(),
                 json.dumps(asdict(result), ensure_ascii=False), request.request_id),
            )
        return result
