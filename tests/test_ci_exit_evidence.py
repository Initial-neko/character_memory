"""CI must retain failing pytest output without masking its exit status."""
import json
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
