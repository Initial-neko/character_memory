from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from character_memory.eval.runner import (
    STATUS_ENGINEERING,
    STATUS_ENVIRONMENT,
    STATUS_PASSED,
    STATUS_SEMANTIC,
    STATUS_UNSCORED,
    STATUSES,
)

REPORT_SCHEMA = "character-memory/eval-report/v1"
REVIEW_SCHEMA = "character-memory/eval-reviews/v1"
COMPARE_SCHEMA = "character-memory/eval-compare/v1"

# The seven independent product domains. A suite declares one with `domain`.
DOMAINS = {
    "B1": "dialogue",
    "B2": "preference",
    "B3": "persona",
    "B4": "memory",
    "B5": "relationship",
    "B6": "activity",
    "B7": "system",
}
REVIEW_KINDS = {"human", "external-judge", "external-model"}
GENERATION_REVIEW_KINDS = {"generation", "runtime", "runner", "same-process"}

_DIFF_FIELDS = (
    "status",
    "contract_passed",
    "actions",
    "messages",
    "recalled_memory_ids",
    "created_memory_ids",
    "memory_decisions",
    "review.verdict",
    "failure.class",
    "cost.model_calls",
    "cost.input_tokens",
    "cost.output_tokens",
    "cost.total_tokens",
    "duration_ms",
    "output_digest",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: str | Path) -> str | None:
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def short_digest(value: str | None) -> str | None:
    return value[:16] if value else None


def module_fingerprint(module) -> str | None:
    """Content fingerprint for a prompt/policy source owner.

    A source hash cannot claim a semantic version, but it is exact: two runs
    with the same fingerprint compiled the same prompt owner, and a changed
    contract cannot hide behind an unchanged model id.
    """
    path = getattr(module, "__file__", None)
    digest = _sha256_file(path) if path else None
    return short_digest(digest)


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_metadata(root: str | Path | None = None) -> dict[str, Any]:
    root = Path(root or Path.cwd())
    commit = _git(root, "rev-parse", "HEAD")
    subject = _git(root, "log", "-1", "--pretty=%s")
    dirty_output = _git(root, "status", "--porcelain", "--untracked-files=no")
    return {
        "commit": commit or "unknown",
        "commit_subject": subject or "",
        "dirty": bool(dirty_output) if dirty_output is not None else None,
    }


def suite_metadata(path: str | Path) -> dict[str, Any]:
    suite_path = Path(path)
    digest = _sha256_file(suite_path)
    cases = 0
    try:
        cases = sum(
            1
            for line in suite_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except OSError:
        pass
    return {
        "path": str(suite_path),
        "sha256": short_digest(digest),
        "case_count": cases,
    }


def build_metadata(
    *,
    suite_path: str | Path,
    model_names,
    persona_versions: dict[str, str] | None = None,
    mode: str = "offline",
    paid_calls_allowed: int = 0,
    root: str | Path | None = None,
) -> dict[str, Any]:
    from character_memory.runtime import context as context_module
    from character_memory.runtime import prompt_parts as prompt_module

    models = sorted({str(name) for name in (model_names or []) if str(name)})
    git = git_metadata(root)
    return {
        "runner_version": "2",
        "commit": git["commit"],
        "commit_subject": git["commit_subject"],
        "dirty": git["dirty"],
        "model": models[0] if len(models) == 1 else ",".join(models),
        "models": models,
        "prompt_version": module_fingerprint(prompt_module),
        "policy_version": module_fingerprint(context_module),
        "persona_versions": dict(sorted((persona_versions or {}).items())),
        "suite": suite_metadata(suite_path),
        "generation": {
            "mode": mode,
            "paid_calls_allowed": int(paid_calls_allowed) if mode == "live" else 0,
        },
    }


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in STATUSES}
    for case in cases:
        status = str(case.get("status") or STATUS_ENGINEERING)
        counts[status] = counts.get(status, 0) + 1
    review_required = sum(1 for case in cases if case.get("review_required"))
    reviewed = sum(
        1
        for case in cases
        if case.get("review_required")
        and (case.get("review") or {}).get("state") == "reviewed"
    )
    reviewed_passed = sum(
        1
        for case in cases
        if case.get("review_required") and case.get("status") == STATUS_PASSED
    )
    return {
        "total": len(cases),
        "status_counts": counts,
        "contract_passed": sum(1 for case in cases if case.get("contract_passed")),
        # Kept explicit so nobody turns engineering green into a quality score.
        "reviewed_passed": reviewed_passed,
        "unscored": counts.get(STATUS_UNSCORED, 0),
        "engineering": counts.get(STATUS_ENGINEERING, 0),
        "environment": counts.get(STATUS_ENVIRONMENT, 0),
        "semantic": counts.get(STATUS_SEMANTIC, 0),
        "passed": counts.get(STATUS_PASSED, 0),
        "review_required": review_required,
        "reviewed": reviewed,
        "semantic_unreviewed": max(0, review_required - reviewed),
    }


def aggregate_costs(cases: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "model_calls",
        "logical_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "token_known_calls",
        "provider_ms",
        "paid_calls",
        "wall_ms",
    )
    totals = {key: 0 for key in keys}
    for case in cases:
        cost = case.get("cost") or {}
        for key in keys:
            if key == "wall_ms":
                totals[key] += float(case.get("duration_ms") or 0.0)
            else:
                totals[key] += float(cost.get(key) or 0.0)
    for key in keys:
        if key in {"provider_ms", "wall_ms"}:
            totals[key] = round(totals[key], 1)
        else:
            totals[key] = int(totals[key])
    calls = totals["model_calls"]
    totals["token_coverage"] = (
        round(totals["token_known_calls"] / calls, 4) if calls else None
    )
    return totals


def coverage_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    declared = [case for case in cases if case.get("domain")]
    if not declared:
        return {"declared": False, "domains": {}, "missing": []}
    counts: dict[str, int] = {}
    for case in declared:
        domain = str(case.get("domain"))
        counts[domain] = counts.get(domain, 0) + 1
    domains = {key: counts.get(key, 0) for key in DOMAINS}
    return {
        "declared": True,
        "domains": domains,
        "missing": [key for key, value in domains.items() if not value],
    }


def output_digest(case: dict[str, Any]) -> str:
    payload = {
        "actions": case.get("actions") or [],
        "messages": case.get("messages") or [],
        "perception": case.get("perception") or "",
        "reaction": case.get("reaction") or "",
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return short_digest(_sha256_bytes(canonical.encode("utf-8")))


def build_report(
    cases: list[dict[str, Any]],
    *,
    label: str,
    metadata: dict[str, Any],
    run_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    normalized = []
    for case in cases:
        item = copy.deepcopy(case)
        item.setdefault("output_digest", output_digest(item))
        normalized.append(item)
    created = created_at or datetime.now(timezone.utc).isoformat()
    run = run_id or (
        "eval-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    return {
        "schema": REPORT_SCHEMA,
        "run_id": run,
        "label": label,
        "created_at": created,
        "metadata": metadata,
        "summary": summarize_cases(normalized),
        "coverage": coverage_summary(normalized),
        "cost": aggregate_costs(normalized),
        "cases": normalized,
    }


def load_report(path: str | Path) -> dict[str, Any]:
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not report.get("run_id"):
        raise ValueError("not an eval report: missing run_id")
    if not isinstance(report.get("cases"), list):
        raise ValueError("not an eval report: missing cases")
    return report


def write_json(path: str | Path, data: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_reviews(path: str | Path) -> dict[str, Any]:
    reviews = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_reviews(reviews)
    return reviews


def validate_reviews(reviews: dict[str, Any]) -> None:
    if not isinstance(reviews, dict):
        raise ValueError("review artifact must be an object")
    if not str(reviews.get("reviewed_run_id") or "").strip():
        raise ValueError("review artifact needs reviewed_run_id")
    reviewer = reviews.get("reviewer")
    if not isinstance(reviewer, dict):
        raise ValueError("review artifact needs a reviewer object")
    kind = str(reviewer.get("kind") or "").strip().lower()
    if kind in GENERATION_REVIEW_KINDS:
        raise ValueError("review must not be produced by the generation process")
    if kind not in REVIEW_KINDS:
        raise ValueError(
            "reviewer.kind must be one of: " + ", ".join(sorted(REVIEW_KINDS))
        )
    if not str(reviewer.get("name") or "").strip():
        raise ValueError("reviewer.name is required")
    cases = reviews.get("cases")
    if not isinstance(cases, dict):
        raise ValueError("review artifact needs a cases object")
    for case_id, entry in cases.items():
        if not isinstance(entry, dict):
            raise ValueError(f"review for {case_id!r} must be an object")
        verdict = str(entry.get("verdict") or "").strip().lower()
        verdict = {"pass": STATUS_PASSED, "fail": STATUS_SEMANTIC}.get(verdict, verdict)
        if verdict not in {STATUS_PASSED, STATUS_SEMANTIC, STATUS_UNSCORED}:
            raise ValueError(f"review verdict for {case_id!r} must be pass, fail or unscored")


def _reviewer_label(reviewer: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(reviewer.get("name") or ""),
        "kind": str(reviewer.get("kind") or ""),
    }


def apply_reviews(report: dict[str, Any], reviews: dict[str, Any]) -> dict[str, Any]:
    """Merge an independent review artifact into a generation report.

    Pure and idempotent: applying the same artifact twice produces an identical
    report. Unreviewed semantic cases stay `unscored`; a failed engineering
    contract cannot be upgraded by a semantic pass.
    """
    validate_reviews(reviews)
    if reviews["reviewed_run_id"] != report.get("run_id"):
        raise ValueError(
            "review artifact belongs to a different run: "
            f"{reviews['reviewed_run_id']!r} != {report.get('run_id')!r}"
        )
    known = {str(case.get("id")) for case in report["cases"]}
    unknown = sorted(set(reviews["cases"]) - known)
    if unknown:
        raise ValueError("review artifact names unknown cases: " + ", ".join(unknown))

    merged = copy.deepcopy(report)
    reviewer = _reviewer_label(reviews["reviewer"])
    artifact_at = reviews.get("created_at")
    for case in merged["cases"]:
        if not case.get("review_required"):
            continue
        entry = reviews["cases"].get(str(case.get("id")))
        if entry is None:
            # The artifact is authoritative: a case it omits has not been
            # reviewed by this reviewer, so it must not inherit a stale verdict.
            if case.get("contract_passed"):
                case["status"] = STATUS_UNSCORED
                case["pass"] = False
                case["failure"] = {
                    "class": STATUS_UNSCORED,
                    "reason": "semantic review required but not performed",
                }
            case["review"] = {
                "required": True,
                "verdict": None,
                "reviewer": None,
                "notes": "",
                "state": "missing",
            }
            continue
        verdict = str(entry.get("verdict") or "").strip().lower()
        verdict = {"pass": STATUS_PASSED, "fail": STATUS_SEMANTIC}.get(verdict, verdict)
        case["review"] = {
            "required": True,
            "verdict": verdict,
            "reviewer": reviewer,
            "notes": str(entry.get("notes") or ""),
            "criteria_results": entry.get("criteria_results") or {},
            "reviewed_at": entry.get("reviewed_at") or artifact_at,
            "state": "reviewed",
        }
        if not case.get("contract_passed"):
            # Engineering truth outranks a semantic opinion.
            continue
        if verdict == STATUS_PASSED:
            case["status"] = STATUS_PASSED
            case["pass"] = True
            case["failure"] = None
        elif verdict == STATUS_SEMANTIC:
            case["status"] = STATUS_SEMANTIC
            case["pass"] = False
            case["failure"] = {
                "class": STATUS_SEMANTIC,
                "reason": "independent semantic review failed",
                "review_notes": case["review"]["notes"],
            }
        else:
            case["status"] = STATUS_UNSCORED
            case["pass"] = False
            case["failure"] = {
                "class": STATUS_UNSCORED,
                "reason": "semantic review required but not performed",
            }

    merged["reviews"] = {
        "schema": reviews.get("schema") or REVIEW_SCHEMA,
        "independent": True,
        "reviewer": reviewer,
        "artifact_created_at": artifact_at,
        "reviewed_cases": sorted(reviews["cases"]),
    }
    merged["summary"] = summarize_cases(merged["cases"])
    merged["coverage"] = coverage_summary(merged["cases"])
    merged["cost"] = aggregate_costs(merged["cases"])
    return merged


def _get_path(value: dict[str, Any], path: str):
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _changed(before: Any, after: Any) -> bool:
    return json.dumps(before, ensure_ascii=False, sort_keys=True) != json.dumps(
        after, ensure_ascii=False, sort_keys=True
    )


def _direction(baseline: dict[str, Any], candidate: dict[str, Any]) -> str:
    before = baseline.get("status")
    after = candidate.get("status")
    if STATUS_ENVIRONMENT in {before, after}:
        # A missing local dependency is not a product improvement/regression.
        return "not_comparable"
    if STATUS_UNSCORED in {before, after}:
        return "still_unscored" if before == after == STATUS_UNSCORED else "not_comparable"
    if before == STATUS_PASSED and after == STATUS_PASSED:
        return "unchanged"
    if before != STATUS_PASSED and after == STATUS_PASSED:
        return "improved"
    if before == STATUS_PASSED and after != STATUS_PASSED:
        return "regressed"
    return "still_failing"


def _cost_delta(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    before = baseline.get("cost") or {}
    after = candidate.get("cost") or {}
    result = {}
    for key in ("model_calls", "input_tokens", "output_tokens", "total_tokens", "paid_calls"):
        result[key] = int(after.get(key) or 0) - int(before.get(key) or 0)
    result["wall_ms"] = round(
        float(candidate.get("duration_ms") or 0.0)
        - float(baseline.get("duration_ms") or 0.0),
        1,
    )
    return result


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Per-case baseline/candidate comparison, never a single pass-rate delta."""
    baseline_cases = {str(case.get("id")): case for case in baseline.get("cases", [])}
    candidate_cases = {str(case.get("id")): case for case in candidate.get("cases", [])}
    case_ids = sorted(set(baseline_cases) | set(candidate_cases))
    rows = []
    counts = {
        "improved": 0,
        "regressed": 0,
        "unchanged": 0,
        "still_failing": 0,
        "still_unscored": 0,
        "not_comparable": 0,
        "case_added": 0,
        "case_removed": 0,
    }
    for case_id in case_ids:
        before = baseline_cases.get(case_id)
        after = candidate_cases.get(case_id)
        if before is None:
            direction = "case_added"
            counts[direction] += 1
            rows.append(
                {
                    "id": case_id,
                    "domain": after.get("domain") if after else None,
                    "baseline_status": None,
                    "candidate_status": after.get("status") if after else None,
                    "direction": direction,
                    "comparable": False,
                    "diffs": [],
                }
            )
            continue
        if after is None:
            direction = "case_removed"
            counts[direction] += 1
            rows.append(
                {
                    "id": case_id,
                    "domain": before.get("domain"),
                    "baseline_status": before.get("status"),
                    "candidate_status": None,
                    "direction": direction,
                    "comparable": False,
                    "diffs": [],
                }
            )
            continue
        direction = _direction(before, after)
        counts[direction] += 1
        diffs = []
        for field in _DIFF_FIELDS:
            old = _get_path(before, field)
            new = _get_path(after, field)
            diffs.append(
                {
                    "field": field,
                    "baseline": old,
                    "candidate": new,
                    "changed": _changed(old, new),
                }
            )
        rows.append(
            {
                "id": case_id,
                "domain": after.get("domain") or before.get("domain"),
                "baseline_status": before.get("status"),
                "candidate_status": after.get("status"),
                "direction": direction,
                "comparable": direction not in {"not_comparable", "still_unscored"},
                "diffs": diffs,
                "cost_delta": _cost_delta(before, after),
            }
        )

    metadata_changes = []
    bmeta = baseline.get("metadata") or {}
    cmeta = candidate.get("metadata") or {}
    for field in ("commit", "model", "prompt_version", "policy_version"):
        if _changed(bmeta.get(field), cmeta.get(field)):
            metadata_changes.append(
                {
                    "field": field,
                    "baseline": bmeta.get(field),
                    "candidate": cmeta.get(field),
                }
            )
    if _changed(bmeta.get("persona_versions"), cmeta.get("persona_versions")):
        metadata_changes.append(
            {
                "field": "persona_versions",
                "baseline": bmeta.get("persona_versions"),
                "candidate": cmeta.get("persona_versions"),
            }
        )
    if _changed((bmeta.get("suite") or {}).get("sha256"), (cmeta.get("suite") or {}).get("sha256")):
        metadata_changes.append(
            {
                "field": "suite.sha256",
                "baseline": (bmeta.get("suite") or {}).get("sha256"),
                "candidate": (cmeta.get("suite") or {}).get("sha256"),
            }
        )

    changed_fields: dict[str, int] = {}
    for row in rows:
        for diff in row.get("diffs", []):
            if diff["changed"]:
                changed_fields[diff["field"]] = changed_fields.get(diff["field"], 0) + 1

    return {
        "schema": COMPARE_SCHEMA,
        "baseline": {
            "run_id": baseline.get("run_id"),
            "label": baseline.get("label"),
            "metadata": baseline.get("metadata"),
            "summary": baseline.get("summary"),
            "cost": baseline.get("cost"),
        },
        "candidate": {
            "run_id": candidate.get("run_id"),
            "label": candidate.get("label"),
            "metadata": candidate.get("metadata"),
            "summary": candidate.get("summary"),
            "cost": candidate.get("cost"),
        },
        "summary": {
            "cases_total": len(case_ids),
            **counts,
            "changed_fields": dict(sorted(changed_fields.items())),
        },
        "metadata_changes": metadata_changes,
        "cases": rows,
    }


def _status_word(status: str | None) -> str:
    return str(status or "missing").upper()


def render_report_text(report: dict[str, Any], case_id: str | None = None) -> str:
    metadata = report.get("metadata") or {}
    summary = report.get("summary") or {}
    cost = report.get("cost") or {}
    suite = metadata.get("suite") or {}
    lines = [
        f"run: {report.get('label')}  id: {report.get('run_id')}",
        f"commit: {metadata.get('commit')}  dirty: {metadata.get('dirty')}  model: {metadata.get('model')}",
        f"prompt: {metadata.get('prompt_version')}  policy: {metadata.get('policy_version')}",
        f"suite: {suite.get('path')}  sha256: {suite.get('sha256')}  cases: {summary.get('total')}",
        "",
        (
            "status: "
            f"passed {summary.get('passed', 0)} | "
            f"unscored {summary.get('unscored', 0)} | "
            f"engineering {summary.get('engineering', 0)} | "
            f"environment {summary.get('environment', 0)} | "
            f"semantic {summary.get('semantic', 0)} | "
            f"total {summary.get('total', 0)}"
        ),
        (
            f"contract checks: {summary.get('contract_passed', 0)}/{summary.get('total', 0)} passed"
            f"  |  reviewed: {summary.get('reviewed', 0)}/{summary.get('review_required', 0)} required"
            f"  |  reviewed passed: {summary.get('reviewed_passed', 0)}"
        ),
        (
            f"cost: model_calls {cost.get('model_calls', 0)} | "
            f"tokens {cost.get('input_tokens', 0)}/{cost.get('output_tokens', 0)} "
            f"(coverage {cost.get('token_coverage')}) | "
            f"provider {cost.get('provider_ms', 0)}ms | wall {cost.get('wall_ms', 0)}ms | "
            f"paid {cost.get('paid_calls', 0)}"
        ),
    ]
    coverage = report.get("coverage") or {}
    if coverage.get("declared"):
        lines.append(
            "coverage: "
            + " ".join(f"{key} {value}" for key, value in coverage.get("domains", {}).items())
            + ("  missing: " + ",".join(coverage.get("missing", [])) if coverage.get("missing") else "  missing: none")
        )
    reviews = report.get("reviews")
    if reviews:
        reviewer = reviews.get("reviewer") or {}
        lines.append(
            f"review artifact: {reviewer.get('kind')}:{reviewer.get('name')} "
            f"independent={reviews.get('independent')}"
        )
    lines.append("")

    cases = report.get("cases", [])
    if case_id:
        matching = [case for case in cases if str(case.get("id")) == case_id]
        if not matching:
            return "\n".join(lines + [f"case not found: {case_id}"])
        case = matching[0]
        lines.extend(_render_case_detail(case))
        return "\n".join(lines)

    width_id = max([len("case")] + [len(str(case.get("id"))) for case in cases])
    lines.append(
        f"{'case'.ljust(width_id)}  domain  status       contract  review      calls  tokens  wall_ms"
    )
    for case in cases:
        review = case.get("review") or {}
        review_text = review.get("verdict") or review.get("state") or "none"
        cost = case.get("cost") or {}
        lines.append(
            f"{str(case.get('id')).ljust(width_id)}  "
            f"{str(case.get('domain') or '-'):<6}  "
            f"{_status_word(case.get('status')):<11}  "
            f"{'PASS' if case.get('contract_passed') else 'FAIL':<8}  "
            f"{str(review_text):<10}  "
            f"{cost.get('model_calls', 0):<5}  "
            f"{cost.get('total_tokens', 0):<6}  "
            f"{case.get('duration_ms', 0)}"
        )
    failures = [case for case in cases if case.get("failure")]
    if failures:
        lines.append("")
        lines.append("failure classes:")
        for case in failures:
            failure = case["failure"]
            lines.append(f"  {case.get('id')}: {failure.get('class')} - {failure.get('reason')}")
    return "\n".join(lines)


def _render_case_detail(case: dict[str, Any]) -> list[str]:
    review = case.get("review") or {}
    failure = case.get("failure") or {}
    cost = case.get("cost") or {}
    lines = [
        f"case: {case.get('id')}  domain: {case.get('domain') or '-'}",
        f"status: {_status_word(case.get('status'))}  contract: {'PASS' if case.get('contract_passed') else 'FAIL'}",
        f"actions: {case.get('actions')}",
        "messages:",
    ]
    messages = case.get("messages") or []
    lines.extend([f"  - {message}" for message in messages] or ["  - <none>"])
    lines.extend(
        [
            f"perception: {case.get('perception') or ''}",
            f"reaction: {case.get('reaction') or ''}",
            f"recalled memory ids: {case.get('recalled_memory_ids')}",
            f"created memory ids: {case.get('created_memory_ids')}",
        ]
    )
    if case.get("checks"):
        lines.append("checks:")
        for check in case["checks"]:
            lines.append(
                f"  - {'PASS' if check.get('passed') else 'FAIL'} {check.get('name')}: {check.get('detail')}"
            )
    if case.get("review_required"):
        lines.append("review criteria:")
        lines.extend([f"  - {criterion}" for criterion in case.get("review_criteria") or []])
        if not review or review.get("state") != "reviewed":
            lines.append("review: UNSCORED - independent semantic review has not been supplied")
        else:
            reviewer = review.get("reviewer") or {}
            lines.append(
                f"review: {_status_word(review.get('verdict'))} by "
                f"{reviewer.get('kind')}:{reviewer.get('name')} ({review.get('reviewed_at')})"
            )
            if review.get("notes"):
                lines.append(f"review notes: {review.get('notes')}")
    else:
        lines.append("review: not required for this contract-only case")
    if failure:
        lines.append(f"failure: {failure.get('class')} - {failure.get('reason')}")
        if failure.get("failed_checks"):
            lines.append(f"failed checks: {failure.get('failed_checks')}")
        if failure.get("message"):
            lines.append(f"error: {failure.get('error_type')}: {failure.get('message')}")
    lines.append(
        "cost: calls {calls} tokens {tokens} coverage {coverage} provider {provider}ms wall {wall}ms paid {paid}".format(
            calls=cost.get("model_calls", 0),
            tokens=cost.get("total_tokens", 0),
            coverage=cost.get("token_coverage"),
            provider=cost.get("provider_ms", 0),
            wall=case.get("duration_ms", 0),
            paid=cost.get("paid_calls", 0),
        )
    )
    return lines


def render_comparison_text(comparison: dict[str, Any], case_id: str | None = None) -> str:
    summary = comparison.get("summary") or {}
    baseline = comparison.get("baseline") or {}
    candidate = comparison.get("candidate") or {}
    lines = [
        f"baseline: {baseline.get('label')} ({baseline.get('run_id')})",
        f"candidate: {candidate.get('label')} ({candidate.get('run_id')})",
        (
            f"cases {summary.get('cases_total')} | improved {summary.get('improved')} | "
            f"regressed {summary.get('regressed')} | unchanged {summary.get('unchanged')} | "
            f"still_failing {summary.get('still_failing')} | still_unscored {summary.get('still_unscored')} | "
            f"not_comparable {summary.get('not_comparable')}"
        ),
        f"changed fields: {summary.get('changed_fields') or '{}'}",
    ]
    changes = comparison.get("metadata_changes") or []
    if changes:
        lines.append("metadata changes:")
        for change in changes:
            lines.append(
                f"  {change.get('field')}: {change.get('baseline')} -> {change.get('candidate')}"
            )
    lines.append("")
    rows = comparison.get("cases", [])
    if case_id:
        rows = [row for row in rows if str(row.get("id")) == case_id]
    for row in rows:
        lines.append(
            f"{row.get('id')} [{row.get('domain') or '-'}] "
            f"{_status_word(row.get('baseline_status'))} -> {_status_word(row.get('candidate_status'))} "
            f"({row.get('direction')})"
        )
        for diff in row.get("diffs", []):
            if diff.get("changed"):
                lines.append(
                    f"  {diff.get('field')}: {diff.get('baseline')!r} -> {diff.get('candidate')!r}"
                )
        if row.get("cost_delta"):
            delta = row["cost_delta"]
            lines.append(
                f"  cost delta: calls {delta.get('model_calls')} tokens {delta.get('total_tokens')} "
                f"wall {delta.get('wall_ms')}ms"
            )
    return "\n".join(lines)
