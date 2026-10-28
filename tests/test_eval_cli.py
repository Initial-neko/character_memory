import contextlib
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from character_memory import cli


def result(case_id, status):
    return {
        "id": case_id,
        "domain": "B1",
        "tags": ["contract"],
        "character_id": "rin",
        "status": status,
        "pass": status == "passed",
        "contract_passed": status != "engineering",
        "checks": [],
        "actions": ["MESSAGE"],
        "messages": ["ok"],
        "message_count": 1,
        "silent": False,
        "perception_present": True,
        "reaction_present": True,
        "perception": "safe",
        "reaction": "safe",
        "recalled_memory_ids": [],
        "created_memory_ids": [],
        "memory_decisions": [],
        "review_required": False,
        "review_criteria": [],
        "review": None,
        "failure": None,
        "cost": {
            "model_calls": 0,
            "logical_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_known_calls": 0,
            "token_coverage": None,
            "provider_ms": 0.0,
            "paid_calls": 0,
        },
        "duration_ms": 1.0,
    }


@pytest.fixture
def eval_environment(monkeypatch):
    settings = Mock()
    settings.model_copy = lambda update: SimpleNamespace(db_path=update["db_path"])
    bundle = SimpleNamespace(runtimes={"rin": SimpleNamespace(model=SimpleNamespace(model="fake"))}, close=Mock())
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "build_offline_eval_bundle", lambda _: bundle)
    monkeypatch.setattr(cli.tempfile, "TemporaryDirectory", lambda **_: contextlib.nullcontext("unused-isolated-path"))
    monkeypatch.setattr(sys, "argv", ["character-memory", "eval", "unused.jsonl"])
    return bundle


@pytest.mark.parametrize(
    "statuses,code",
    [
        (["passed"], 0),
        (["passed", "unscored"], 1),
        (["engineering"], 1),
        (["environment"], 1),
        ([], 2),
    ],
)
def test_eval_cli_exit_matches_report(eval_environment, monkeypatch, capsys, statuses, code):
    results = [result(f"case-{index}", status) for index, status in enumerate(statuses)]
    monkeypatch.setattr(cli.EvalRunner, "run_jsonl", lambda *_: results)
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == code
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] == sum(1 for status in statuses if status == "passed")
    assert report["total"] == len(results)
    assert report["unscored"] == sum(1 for status in statuses if status == "unscored")
    assert report["results"] == results
    assert report["status_counts"]["passed"] == sum(1 for status in statuses if status == "passed")
    eval_environment.close.assert_called_once()


def test_eval_defaults_to_offline_and_never_builds_the_paid_app(monkeypatch, capsys):
    settings = Mock()
    settings.model_copy = lambda update: SimpleNamespace(db_path=update["db_path"])
    bundle = SimpleNamespace(runtimes={}, close=Mock())
    calls = []
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "build_offline_eval_bundle", lambda _: calls.append("offline") or bundle)
    monkeypatch.setattr(cli, "build_app_from_settings", lambda *_: pytest.fail("paid app was built by default"))
    monkeypatch.setattr(cli, "EvalRunner", lambda *_, **__: SimpleNamespace(run_jsonl=lambda *_: []))
    monkeypatch.setattr(cli.tempfile, "TemporaryDirectory", lambda **_: contextlib.nullcontext("unused"))
    monkeypatch.setattr(sys, "argv", ["character-memory", "eval", "unused.jsonl"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    assert calls == ["offline"]
    report = json.loads(capsys.readouterr().out)
    assert report["paid_calls"] == 0


@pytest.mark.parametrize("error", [ValueError("invalid JSON"), KeyError("unknown character"), RuntimeError("runner failed")])
def test_eval_failure_closes_bundle_without_success_report(eval_environment, monkeypatch, capsys, error):
    def fail(*_):
        raise error
    monkeypatch.setattr(cli.EvalRunner, "run_jsonl", fail)
    with pytest.raises(type(error)):
        cli.main()
    assert capsys.readouterr().out == ""
    eval_environment.close.assert_called_once()


@pytest.mark.parametrize("status,expected", [("passed", 0), ("unscored", 1)])
def test_eval_main_propagates_status_to_real_process(status, expected):
    # Exercise main's real process exit; only the model/config/storage boundary is fake.
    script = '''
import contextlib, sys
from types import SimpleNamespace
from unittest.mock import Mock
from character_memory import cli
settings = Mock()
settings.model_copy = lambda update: SimpleNamespace(db_path=update["db_path"])
bundle = SimpleNamespace(runtimes={"rin": SimpleNamespace(model=SimpleNamespace(model="fake"))}, close=lambda: None)
cli.load_settings = lambda _: settings
cli.build_offline_eval_bundle = lambda _: bundle
cli.tempfile.TemporaryDirectory = lambda **_: contextlib.nullcontext("unused-isolated-path")
cli.EvalRunner.run_jsonl = lambda *_: [{
    "id": "one", "domain": "B1", "tags": [], "character_id": "rin",
    "status": STATUS, "pass": STATUS == "passed", "contract_passed": True,
    "checks": [], "actions": [], "messages": [], "message_count": 0,
    "silent": True, "perception_present": False, "reaction_present": False,
    "perception": "", "reaction": "", "recalled_memory_ids": [],
    "created_memory_ids": [], "memory_decisions": [], "review_required": False,
    "review_criteria": [], "review": None, "failure": None,
    "cost": {"model_calls": 0, "logical_calls": 0, "input_tokens": 0,
             "output_tokens": 0, "total_tokens": 0, "token_known_calls": 0,
             "token_coverage": None, "provider_ms": 0.0, "paid_calls": 0},
    "duration_ms": 1.0,
}]
sys.argv = ["character-memory", "eval", "unused.jsonl"]
cli.main()
'''.replace("STATUS", repr(status))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == expected, result.stderr
    assert json.loads(result.stdout)["status_counts"]["passed"] == int(status == "passed")
