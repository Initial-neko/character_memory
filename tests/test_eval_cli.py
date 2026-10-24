import contextlib
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from character_memory import cli


@pytest.fixture
def eval_environment(monkeypatch):
    settings = Mock()
    bundle = SimpleNamespace(runtime=object(), close=Mock())
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "build_app_from_settings", lambda _: bundle)
    monkeypatch.setattr(cli.tempfile, "TemporaryDirectory", lambda **_: contextlib.nullcontext("unused-isolated-path"))
    monkeypatch.setattr(sys, "argv", ["character-memory", "eval", "unused.jsonl"])
    return bundle


@pytest.mark.parametrize("outcomes,code", [([True], 0), ([True, False], 1), ([False], 1), ([], 2)])
def test_eval_cli_exit_matches_report(eval_environment, monkeypatch, capsys, outcomes, code):
    results = [{"id": str(i), "tags": ["contract"], "pass": value} for i, value in enumerate(outcomes)]
    monkeypatch.setattr(cli.EvalRunner, "run_jsonl", lambda *_: results)
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == code
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] == sum(outcomes)
    assert report["total"] == len(outcomes)
    assert report["results"] == results
    assert report["by_tag"] == ({"contract": {"passed": sum(outcomes), "total": len(outcomes)}} if outcomes else {})
    eval_environment.close.assert_called_once()


@pytest.mark.parametrize("error", [ValueError("invalid JSON"), KeyError("unknown character"), RuntimeError("runner failed")])
def test_eval_failure_closes_bundle_without_success_report(eval_environment, monkeypatch, capsys, error):
    def fail(*_):
        raise error
    monkeypatch.setattr(cli.EvalRunner, "run_jsonl", fail)
    with pytest.raises(type(error)):
        cli.main()
    assert capsys.readouterr().out == ""
    eval_environment.close.assert_called_once()


@pytest.mark.parametrize("passed,expected", [(True, 0), (False, 1)])
def test_eval_main_propagates_status_to_real_process(passed, expected):
    # Exercise main's real process exit; only the model/config/storage boundary is fake.
    script = '''
import contextlib, sys
from types import SimpleNamespace
from unittest.mock import Mock
from character_memory import cli
cli.load_settings = lambda _: Mock()
cli.build_app_from_settings = lambda _: SimpleNamespace(runtime=object(), close=lambda: None)
cli.tempfile.TemporaryDirectory = lambda **_: contextlib.nullcontext("unused-isolated-path")
cli.EvalRunner.run_jsonl = lambda *_: [{"id":"one", "tags":[], "pass": PASSED}]
sys.argv = ["character-memory", "eval", "unused.jsonl"]
cli.main()
'''.replace("PASSED", repr(passed))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == expected, result.stderr
    assert json.loads(result.stdout)["passed"] == int(passed)
