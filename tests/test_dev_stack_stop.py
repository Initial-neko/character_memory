"""`character-stack --stop` finds the launcher through its state files.

These drive the stop logic with a fake liveness probe and a fake killer: a real
one would either take 20 seconds or kill the process running the tests.
"""

from __future__ import annotations

import threading
import time

from character_memory.dev_stack import stack_status, stop_running_stack


def _paths(tmp_path):
    return tmp_path / "stack.pid", tmp_path / "stack.stop"


def test_stop_without_a_pid_file_reports_not_running(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    killed = []
    assert stop_running_stack(
        pid_file=pid_file, stop_file=stop_file, force_kill=killed.append
    ) == "stack: not running"
    assert killed == []
    assert not stop_file.exists()


def test_stop_cleans_up_a_pid_file_whose_process_is_gone(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    pid_file.write_text("4242\n", encoding="utf-8")
    killed = []
    message = stop_running_stack(
        pid_file=pid_file, stop_file=stop_file, is_alive=lambda _pid: False, force_kill=killed.append
    )
    assert message == "stack: not running (pid 4242 is gone)"
    assert not pid_file.exists()
    assert killed == []


def test_stop_cleans_up_an_unreadable_pid_file(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    pid_file.write_text("not-a-pid\n", encoding="utf-8")
    message = stop_running_stack(pid_file=pid_file, stop_file=stop_file)
    assert message == "stack: not running (removed an unreadable pid file)"
    assert not pid_file.exists()


def test_stop_waits_for_the_launcher_to_remove_its_own_pid_file(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    pid_file.write_text("4242\n", encoding="utf-8")
    killed = []

    def launcher():
        # The real launcher honours the request inside its 1s main loop.
        while not stop_file.exists():
            time.sleep(0.01)
        pid_file.unlink()

    threading.Thread(target=launcher, daemon=True).start()
    message = stop_running_stack(
        pid_file=pid_file, stop_file=stop_file, timeout=5.0,
        is_alive=lambda _pid: True, force_kill=killed.append,
    )
    assert message == "stack: stopped"
    assert killed == []


def test_stop_forces_the_tree_when_the_launcher_ignores_the_request(tmp_path):
    pid_file, stop_file = _paths(tmp_path)
    pid_file.write_text("4242\n", encoding="utf-8")
    killed = []
    message = stop_running_stack(
        pid_file=pid_file, stop_file=stop_file, timeout=0.3,
        is_alive=lambda _pid: True, force_kill=killed.append,
    )
    assert "forced stop" in message
    assert killed == [4242]


def test_status_distinguishes_missing_stale_and_live_pid_files(tmp_path):
    pid_file, _stop_file = _paths(tmp_path)
    assert stack_status(pid_file=pid_file) == "stack: not running"

    pid_file.write_text("4242\n", encoding="utf-8")
    assert stack_status(pid_file=pid_file, is_alive=lambda _pid: False) == (
        "stack: not running (stale pid file for pid 4242)"
    )
    assert stack_status(pid_file=pid_file, is_alive=lambda _pid: True) == "stack: running (pid 4242)"
