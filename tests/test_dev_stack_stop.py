"""`character-stack --stop` finds the launcher through its state files.

These drive stop logic with a fake liveness probe. Shutdown never force-kills
an arbitrary PID from a potentially stale state file.
"""

from __future__ import annotations

import threading
import time

from character_memory.dev_stack import claim_stack_state, stack_status, stop_running_stack


def _paths(tmp_path):
    return tmp_path / "stack.pid", tmp_path / "stack.stop"


def test_stop_without_a_pid_file_reports_not_running(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    assert stop_running_stack(pid_file=pid_file, stop_file=stop_file) == "stack: not running"
    assert not stop_file.exists()


def test_stop_cleans_up_a_pid_file_whose_process_is_gone(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    pid_file.write_text("4242\n", encoding="utf-8")
    message = stop_running_stack(
        pid_file=pid_file, stop_file=stop_file, is_alive=lambda _pid: False
    )
    assert message == "stack: not running (pid 4242 is gone)"
    assert not pid_file.exists()


def test_stop_cleans_up_an_unreadable_pid_file(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    pid_file.write_text("not-a-pid\n", encoding="utf-8")
    message = stop_running_stack(pid_file=pid_file, stop_file=stop_file)
    assert message == "stack: not running (removed an unreadable pid file)"
    assert not pid_file.exists()


def test_stop_waits_for_the_launcher_to_remove_its_own_pid_file(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    pid_file.write_text("4242\n", encoding="utf-8")

    def launcher():
        # The real launcher honours the request inside its 1s main loop.
        while not stop_file.exists():
            time.sleep(0.01)
        pid_file.unlink()

    threading.Thread(target=launcher, daemon=True).start()
    message = stop_running_stack(
        pid_file=pid_file, stop_file=stop_file, timeout=5.0,
        is_alive=lambda _pid: True,
    )
    assert message == "stack: stopped"
    assert killed == []


def test_stop_never_kills_pid_when_launcher_ignores_the_request(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    pid_file.write_text("4242\n", encoding="utf-8")
    message = stop_running_stack(
        pid_file=pid_file, stop_file=stop_file, timeout=0.3,
        is_alive=lambda _pid: True, force_kill=killed.append,
    )
    assert "no process was killed" in message
    assert pid_file.exists()
    assert stop_file.exists()


def test_status_distinguishes_missing_stale_and_live_pid_files(tmp_path):
    pid_file, _stop_file = _paths(tmp_path)
    assert stack_status(pid_file=pid_file) == "stack: not running"

    pid_file.write_text("4242\n", encoding="utf-8")
    assert stack_status(pid_file=pid_file, is_alive=lambda _pid: False) == (
        "stack: not running (stale pid file for pid 4242)"
    )
    assert stack_status(pid_file=pid_file, is_alive=lambda _pid: True) == "stack: running (pid 4242)"


def test_claim_stack_state_does_not_replace_another_launcher(tmp_path):
    import os
    import pytest

    pid_file, stop_file = _paths(tmp_path)
    claim_stack_state(pid_file=pid_file, stop_file=stop_file)
    assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
    with pytest.raises(RuntimeError, match="already exists"):
        claim_stack_state(pid_file=pid_file, stop_file=stop_file)
    assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_claim_requires_explicit_stale_pid_cleanup(tmp_path):
    import pytest

    pid_file, stop_file = _paths(tmp_path)
    pid_file.write_text("999999\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="already exists"):
        claim_stack_state(pid_file=pid_file, stop_file=stop_file)
    assert stop_running_stack(pid_file=pid_file, stop_file=stop_file, is_alive=lambda _: False).startswith("stack: not running")
    claim_stack_state(pid_file=pid_file, stop_file=stop_file)
    assert pid_file.exists()
