import shutil
import subprocess
from pathlib import Path

import pytest


def test_live2d_runtime_lifecycle_and_interactions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the browser-independent runtime check")
    result = subprocess.run([node, str(Path(__file__).with_name("live2d_runtime_checks.cjs"))], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live2d_automatic_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the browser-independent behavior check")
    result = subprocess.run([node, str(Path(__file__).with_name("live2d_behavior_checks.cjs"))], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
