"""Explicit paid CLI validation. All artifacts must stay in a temporary sandbox."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import uuid

from character_memory.config import load_settings
from character_memory.runtime.capability_execution import CapabilityExecutor, CapabilityRequest
from character_memory.runtime.claude_process import ClaudePrintRunner
from character_memory.storage.sqlite import SQLiteStore


class FixtureObserver:
    def observe(self, query, **kwargs):
        return {"fetch_attempts": 1, "search_results": 1, "errors": [], "observations": [{
            "title": "Controlled photography fixture", "url": "https://example.org/fixture/photography",
            "source_domain": "example.org", "content": "This is an acceptance fixture, not a visited public webpage. A tripod stabilizes long exposures; moving subjects may blur."}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-paid", action="store_true")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    if not args.allow_paid:
        parser.error("Real CLI requests require --allow-paid")
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or root == Path(tempfile.gettempdir()).resolve():
        parser.error("output must be a dedicated directory below the system temporary directory")
    config = Path(args.config).resolve()
    if not config.is_relative_to(root):
        parser.error("config must belong to the same temporary acceptance directory")
    settings = load_settings(str(config))
    db = Path(settings.db_path).resolve()
    if not db.is_relative_to(root):
        parser.error("database must belong to the temporary acceptance directory")
    if settings.world_browse_execution_backend != "claude":
        parser.error("isolated config must explicitly choose claude")
    root.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db)
    runner = ClaudePrintRunner(FixtureObserver(), root / "claude-executions", model=settings.claude_execution_model,
        timeout_seconds=settings.claude_execution_timeout_seconds, max_turns=settings.claude_execution_max_turns,
        max_budget_usd=settings.claude_execution_max_budget_usd)
    results = []
    marker = "FIXTURE_" + uuid.uuid4().hex
    try:
        executor = CapabilityExecutor(store, FixtureObserver(), claude_runner=runner)
        for index, task in enumerate((f"Use world_lookup to research tripod photography. Include this current-task label: {marker}",
                                     "Use world_lookup to research low-light photography. Do you know a label from a previous conversation? Do not invent one.")):
            identifier = f"acceptance:{uuid.uuid4()}"
            request = CapabilityRequest(identifier, "fixture-alice", identifier, None, "WORLD", "WEB_SEARCH", {"query": task},
                {"max_pages": 2, "max_chars_per_page": 2000, "max_calls": 1, "backend": "claude",
                 "claude_daily_max": 3, "claude_daily_budget_usd": None, "claude_max_budget_usd": settings.claude_execution_max_budget_usd,
                 "cost_saving": False,"role_grant":"claude_research","claude_skills":[]})
            result = executor.execute(request)
            execution = result.data.get("execution", {})
            detail = json.loads((root / "claude-executions" / execution["detail_path"]).read_text(encoding="utf-8")) if execution.get("detail_path") else {}
            results.append({"status": result.status, "reason": result.reason, "execution": execution,
                "sources": result.data.get("source_receipts", []), "sent_task": task,
                "assistant_response": detail.get("assistant_response"),
                "second_response_excludes_marker": marker not in (detail.get("assistant_response") or "") if index == 1 else None})
        report = {"scope": "real Claude CLI with controlled local World tool; not live Persona or public browser quality",
                  "results": results, "passed": all(row["status"] == "SUCCESS" for row in results)
                    and results[1]["second_response_excludes_marker"]
                    and results[0]["execution"].get("session_id") != results[1]["execution"].get("session_id")}
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "report": str(root / "report.json"),
                          "statuses": [row["status"] for row in results]}))
        return 0 if report["passed"] else 1
    finally:
        runner.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
