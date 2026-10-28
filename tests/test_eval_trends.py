import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from character_memory.config import Settings
from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    PersonReaction,
)
from character_memory.eval.offline import (
    BudgetedModel,
    PaidCallBudgetExceeded,
    build_offline_eval_bundle,
    persona_versions_from_runtimes,
)
from character_memory.eval.report import (
    DOMAINS,
    apply_reviews,
    build_metadata,
    build_report,
    compare_reports,
    render_comparison_text,
    render_report_text,
)
from character_memory.eval.runner import (
    EvalCostMeter,
    EvalRunner,
    STATUS_ENGINEERING,
    STATUS_ENVIRONMENT,
    STATUS_PASSED,
    STATUS_SEMANTIC,
    STATUS_UNSCORED,
)


class FakeStore:
    def get_runtime_trace(self, source_event_id):
        return {}


class FakeRuntime:
    def __init__(self, *, reaction=None, error=None, meter=None, usage=None):
        self.store = FakeStore()
        self.error = error
        self.meter = meter
        self.usage = usage
        self.reaction = reaction or PersonReaction(
            perception="safe",
            reaction="safe",
            actions=[ActionDecision(type=ActionType.MESSAGE, message="ok")],
        )

    def handle(self, event):
        if self.meter is not None and self.usage is not None:
            self.meter.record(self.usage)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            event=event.model_copy(update={"id": 1}),
            reaction=self.reaction,
            recalled_memories=[],
            created_memory_ids=[],
            context="# Delivery Context\ncurrent",
        )


def make_case(case_id="case", **overrides):
    case = {
        "id": case_id,
        "domain": "B1",
        "tags": ["contract"],
        "event_time": "2026-10-01T10:00:00+00:00",
        "content": "hello",
    }
    case.update(overrides)
    return case


def run_case(case, runtime=None, meter=None, reviews=None):
    runtime = runtime or FakeRuntime()
    return EvalRunner(runtime, meter=meter, reviews=reviews).run_case(case)


def report_from(cases, *, run_id="run-1", label="baseline", metadata=None):
    return build_report(
        cases,
        label=label,
        metadata=metadata or {"commit": "abc", "model": "test", "prompt_version": "p1", "policy_version": "q1"},
        run_id=run_id,
        created_at="2026-10-01T00:00:00+00:00",
    )


def test_failure_classes_stay_separate():
    engineering = run_case(
        make_case(),
        FakeRuntime(error=RuntimeError("boom")),
    )
    environment = run_case(
        make_case(),
        FakeRuntime(error=ImportError("optional dependency missing")),
    )
    missing_runtime = EvalRunner({}).run_case(make_case())
    contract = run_case(
        make_case(allowed_actions=["EMOJI"]),
        FakeRuntime(
            reaction=PersonReaction(
                actions=[ActionDecision(type=ActionType.MESSAGE, message="ok")]
            )
        ),
    )
    unscored = run_case(make_case(review_criteria=["must be natural"]))
    semantic = run_case(
        make_case(review_criteria=["must be natural"]),
        reviews={"case": {"verdict": "fail", "notes": "wrong tone"}},
    )

    assert engineering["status"] == STATUS_ENGINEERING
    assert engineering["failure"]["class"] == STATUS_ENGINEERING
    assert environment["status"] == STATUS_ENVIRONMENT
    assert environment["failure"]["error_type"] == "ImportError"
    assert missing_runtime["status"] == STATUS_ENVIRONMENT
    assert "unknown eval character_id" in missing_runtime["failure"]["message"]
    assert contract["status"] == STATUS_ENGINEERING
    assert contract["contract_passed"] is False
    assert contract["failure"]["failed_checks"] == ["allowed_actions"]
    assert unscored["status"] == STATUS_UNSCORED
    assert unscored["pass"] is False
    assert unscored["contract_passed"] is True
    assert semantic["status"] == STATUS_SEMANTIC
    assert semantic["failure"]["class"] == STATUS_SEMANTIC


def test_review_cannot_upgrade_a_failed_contract():
    case = make_case(allowed_actions=["EMOJI"], review_criteria=["criterion"])
    result = run_case(case, reviews={"case": {"verdict": "pass"}})
    assert result["status"] == STATUS_ENGINEERING
    assert result["contract_passed"] is False


def test_b1_b7_suite_executes_every_domain_offline(tmp_path):
    suite = Path("evals/b1_b7.jsonl")
    cases = [json.loads(line) for line in suite.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {case["domain"] for case in cases} == set(DOMAINS)
    assert len(cases) == len(DOMAINS)

    settings = Settings(db_path=str(tmp_path / "eval.db"))
    bundle = build_offline_eval_bundle(settings)
    try:
        results = EvalRunner(bundle.runtimes).run_jsonl(suite)
    finally:
        bundle.close()

    assert [result["id"] for result in results] == [case["id"] for case in cases]
    assert all(result["contract_passed"] for result in results)
    assert all(result["status"] in {STATUS_PASSED, STATUS_UNSCORED} for result in results)
    assert all(result["status"] != STATUS_ENVIRONMENT for result in results)
    assert all(result["status"] != STATUS_ENGINEERING for result in results)
    assert sum(1 for result in results if result["status"] == STATUS_UNSCORED) == 6
    assert sum(1 for result in results if result["status"] == STATUS_PASSED) == 1


def test_unscored_is_visible_and_never_counted_as_green(tmp_path):
    case = make_case("needs-review", review_criteria=["natural persona"])
    result = run_case(case)
    report = report_from([result])
    text = render_report_text(report, "needs-review")

    assert result["pass"] is False
    assert report["summary"]["passed"] == 0
    assert report["summary"]["unscored"] == 1
    assert report["summary"]["reviewed_passed"] == 0
    assert "UNSCORED" in text
    assert "review: UNSCORED" in text
    assert "reviewed: 0/1 required" in text
    assert "status: passed 0" in text


def test_cost_record_tracks_calls_tokens_coverage_and_versions(tmp_path):
    meter = EvalCostMeter(paid=True)
    usage = {
        "logical_call_id": "call-1",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "duration_ms": 2.5,
        "model": "paid-test",
    }
    result = run_case(make_case(max_messages=1), FakeRuntime(meter=meter, usage=usage), meter=meter)
    assert result["cost"]["model_calls"] == 1
    assert result["cost"]["logical_calls"] == 1
    assert result["cost"]["input_tokens"] == 10
    assert result["cost"]["output_tokens"] == 5
    assert result["cost"]["total_tokens"] == 15
    assert result["cost"]["token_coverage"] == 1.0
    assert result["cost"]["paid_calls"] == 1

    unknown_meter = EvalCostMeter(paid=False)
    unknown = dict(usage, logical_call_id="call-2", total_tokens=None, input_tokens=None, output_tokens=None)
    second = run_case(
        make_case("unknown", max_messages=1),
        FakeRuntime(meter=unknown_meter, usage=unknown),
        meter=unknown_meter,
    )
    assert second["cost"]["total_tokens"] == 0
    assert second["cost"]["token_coverage"] == 0.0
    assert second["cost"]["paid_calls"] == 0

    metadata = build_metadata(
        suite_path=Path("evals/b1_b7.jsonl"),
        model_names=["offline-deterministic-v1"],
        persona_versions={"rin": "persona-1"},
        mode="offline",
    )
    assert metadata["commit"]
    assert metadata["model"] == "offline-deterministic-v1"
    assert metadata["prompt_version"]
    assert metadata["policy_version"]
    assert metadata["suite"]["case_count"] == 7
    assert metadata["persona_versions"] == {"rin": "persona-1"}


def test_offline_bundle_exposes_persona_versions(tmp_path):
    bundle = build_offline_eval_bundle(Settings(db_path=str(tmp_path / "eval.db")))
    try:
        versions = persona_versions_from_runtimes(bundle.runtimes)
    finally:
        bundle.close()
    assert "rin" in versions
    assert len(versions["rin"]) == 16


def test_compare_reports_returns_per_case_diffs_not_only_rates():
    baseline_case = {
        "id": "case-1",
        "domain": "B1",
        "status": STATUS_PASSED,
        "pass": True,
        "contract_passed": True,
        "review_required": False,
        "actions": ["MESSAGE"],
        "messages": ["hello"],
        "recalled_memory_ids": [],
        "created_memory_ids": [],
        "memory_decisions": [],
        "failure": None,
        "duration_ms": 10.0,
        "cost": {"model_calls": 1, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    changed_case = copy.deepcopy(baseline_case)
    changed_case.update(
        {
            "id": "case-2",
            "status": STATUS_SEMANTIC,
            "pass": False,
            "messages": ["wrong"],
            "failure": {"class": STATUS_SEMANTIC, "reason": "independent semantic review failed"},
            "duration_ms": 20.0,
        }
    )
    candidate_case = copy.deepcopy(baseline_case)
    candidate_case.update(messages=["hello again"], cost={"model_calls": 2, "input_tokens": 20, "output_tokens": 5, "total_tokens": 25})
    baseline = report_from([baseline_case, changed_case], run_id="base")
    candidate = report_from([candidate_case], run_id="cand", label="candidate")

    comparison = compare_reports(baseline, candidate)
    rows = {row["id"]: row for row in comparison["cases"]}
    assert rows["case-1"]["direction"] == "unchanged"
    changed = {diff["field"]: diff for diff in rows["case-1"]["diffs"]}
    assert changed["messages"]["changed"] is True
    assert changed["cost.total_tokens"]["changed"] is True
    assert rows["case-1"]["cost_delta"]["total_tokens"] == 10
    assert rows["case-2"]["direction"] == "case_removed"
    assert comparison["summary"]["case_removed"] == 1
    assert comparison["summary"]["changed_fields"]["messages"] == 1
    assert comparison["metadata_changes"] == []

    text = render_comparison_text(comparison, "case-1")
    assert "messages" in text
    assert "hello" in text and "hello again" in text

    environment_candidate = copy.deepcopy(candidate_case)
    environment_candidate["status"] = STATUS_ENVIRONMENT
    env_comparison = compare_reports(
        report_from([baseline_case], run_id="base-env"),
        report_from([environment_candidate], run_id="cand-env", label="candidate"),
    )
    assert env_comparison["cases"][0]["direction"] == "not_comparable"
    assert env_comparison["summary"]["regressed"] == 0


def test_review_artifact_is_independent_and_idempotent():
    case = make_case("reviewed", review_criteria=["natural persona"])
    engineering = make_case("contract", allowed_actions=["EMOJI"], review_criteria=["criterion"])
    report = report_from([run_case(case), run_case(engineering)])
    reviews = {
        "schema": "character-memory/eval-reviews/v1",
        "reviewed_run_id": report["run_id"],
        "reviewer": {"kind": "human", "name": "reviewer-a"},
        "created_at": "2026-10-02T00:00:00+00:00",
        "cases": {
            "reviewed": {"verdict": "pass", "notes": "natural"},
            "contract": {"verdict": "pass"},
        },
    }
    merged = apply_reviews(report, reviews)
    by_id = {item["id"]: item for item in merged["cases"]}
    assert by_id["reviewed"]["status"] == STATUS_PASSED
    assert by_id["reviewed"]["review"]["reviewer"]["kind"] == "human"
    # A semantic pass cannot hide a failed deterministic contract.
    assert by_id["contract"]["status"] == STATUS_ENGINEERING
    assert merged["reviews"]["independent"] is True
    assert apply_reviews(report, reviews) == merged
    assert compare_reports(report, merged) == compare_reports(report, merged)
    rebuilt = report_from(report["cases"], run_id=report["run_id"])
    assert rebuilt["cases"] == report["cases"]
    assert rebuilt["summary"] == report["summary"]

    wrong_run = dict(reviews, reviewed_run_id="another-run")
    with pytest.raises(ValueError, match="different run"):
        apply_reviews(report, wrong_run)

    same_process = dict(reviews, reviewer={"kind": "generation", "name": "runner"})
    with pytest.raises(ValueError, match="generation process"):
        apply_reviews(report, same_process)

    unknown = dict(reviews, cases={"missing-case": {"verdict": "pass"}})
    with pytest.raises(ValueError, match="unknown cases"):
        apply_reviews(report, unknown)


def test_paid_budget_blocks_before_the_limit_is_crossed():
    class Inner:
        model = "paid-test"

        def __init__(self):
            self.calls = 0

        def react_call_for_session(self, context, session_id):
            self.calls += 1
            return f"reply-{self.calls}"

        def close(self):
            pass

    inner = Inner()
    model = BudgetedModel(inner, 2)
    assert model.react_call_for_session("a", "s") == "reply-1"
    assert model.react_call_for_session("a", "s") == "reply-2"
    with pytest.raises(PaidCallBudgetExceeded):
        model.react_call_for_session("a", "s")
    assert inner.calls == 2


def test_eval_report_and_compare_cli_entry_points(tmp_path, capsys):
    from character_memory import cli

    passing = run_case(make_case("one", allowed_actions=["MESSAGE"]))
    report = report_from([passing], run_id="base")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    assert cli._run_eval_report(str(report_path), case_id="one") == 0
    output = capsys.readouterr().out
    assert "case: one" in output
    assert "status: PASSED" in output

    failed = run_case(make_case("one", allowed_actions=["EMOJI"]))
    candidate = report_from([failed], run_id="cand", label="candidate")
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")

    assert cli._run_eval_compare(str(report_path), str(candidate_path)) == 1
    output = capsys.readouterr().out
    assert "PASSED -> ENGINEERING" in output
    assert "regressed" in output.lower()
