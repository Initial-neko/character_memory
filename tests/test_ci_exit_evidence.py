"""CI must retain failing pytest output without masking its exit status."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="Linux CI uses bash")
@pytest.mark.parametrize("status", [0, 1, 139])
def test_linux_ci_retains_output_and_original_exit_code(tmp_path, status):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(s for s in workflow["jobs"]["test"]["steps"] if s["name"] == "Run tests")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env python3\nimport os,sys\nprint('diagnostic-marker', os.environ.get('PYTHONFAULTHANDLER'))\nsys.exit("+str(status)+")\n")
    uv.chmod(0o755)
    evidence = tmp_path / "artifacts/core-visual-evidence"
    evidence.mkdir(parents=True)
    env = {**os.environ, **step.get("env", {}), "PATH": str(bin_dir)+os.pathsep+os.environ["PATH"]}
    result = subprocess.run([shutil.which("bash"), "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == status
    assert (evidence / "pytest-linux-exit-code.txt").read_text().strip() == str(status)
    assert "diagnostic-marker 1" in (evidence / "pytest-linux.log").read_text()
    upload = next(s for s in workflow["jobs"]["test"]["steps"] if s["name"] == "Upload Linux test results")
    assert upload["if"] == "always()"
    assert upload["with"]["path"].rstrip("/") == "artifacts/core-visual-evidence"


@pytest.mark.skipif(os.name == "nt", reason="Reproduce Linux interpreter-exit abort without a core dump")
def test_faulthandler_survives_pytest_and_records_exit_stage_abort(tmp_path):
    probe = tmp_path / "test_exit_probe.py"
    probe.write_text("import atexit,os,resource\nresource.setrlimit(resource.RLIMIT_CORE,(0,0))\ndef test_ok():\n    atexit.register(os.abort)\n")
    env = {**os.environ, "PYTHONFAULTHANDLER": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", str(probe)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "1 passed" in result.stdout
    assert "Fatal Python error: Aborted" in result.stderr


def _collect_nodes(arguments):
    env={**os.environ,'PYTHONIOENCODING':'utf-8'}
    result=subprocess.run([sys.executable,'-m','pytest','--collect-only','-q',*arguments],
        cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    return {line.replace('\\','/') for line in result.stdout.splitlines()
            if line.startswith('tests') and '::' in line}


def _ci_selection(step):
    import shlex
    args=shlex.split(step['run'])
    args=args[args.index('pytest')+1:]
    selected=[]
    iterator=iter(args)
    for value in iterator:
        if value=='--browser':
            next(iterator)
        elif not value.startswith('--junitxml=') and value!='-q':
            selected.append(value)
    return selected


def test_ci_browser_selection_covers_all_collected_browser_nodes():
    workflow=yaml.safe_load((ROOT/'.github/workflows/ci.yml').read_text())
    step=next(s for s in workflow['jobs']['browser-smoke']['steps'] if s['name']=='Run browser smoke')
    expected=_collect_nodes(['-m','browser'])
    actual=_collect_nodes(_ci_selection(step))
    assert expected,'browser collection must not be empty'
    assert not expected-actual, 'Browser nodes missing from CI: '+repr(sorted(expected-actual))
    assert step['env']['RUN_PLAYWRIGHT']=='1'


def test_windows_ci_runs_native_browser_process_tree_reaping():
    workflow=yaml.safe_load((ROOT/'.github/workflows/ci.yml').read_text())
    job=workflow['jobs']['windows-smoke']
    step=next(s for s in job['steps'] if s['name']=='Run Windows-sensitive smoke tests')
    target='tests/test_browser_session.py::test_real_chromium_close_reaps_entire_windows_process_tree'
    assert _collect_nodes([target]) <= _collect_nodes(_ci_selection(step))
    assert {**job.get('env',{}),**step.get('env',{})}.get('RUN_PLAYWRIGHT')=='1'
    assert any('playwright install chromium' in s.get('run','') for s in job['steps'])
    assert any('--extra browser' in s.get('run','') for s in job['steps'])
