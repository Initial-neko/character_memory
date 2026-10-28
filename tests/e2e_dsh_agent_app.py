"""Independent DSH listener for real-HTTP acceptance.

Uses the production ``create_dsh_app`` factory. Two test-only control routes
outside ``/v2/dsh`` inject a write failure and a write delay so the acceptance
suite can exercise rollback and late-commit semantics on the real code path.
"""
from __future__ import annotations

import os
import threading
import time

from character_memory.dsh_server import create_dsh_app

_state = {"fail_next": False, "delay_next_seconds": 0.0}
_lock = threading.Lock()


def _write_guard() -> None:
    with _lock:
        should_fail = _state["fail_next"]
        delay = _state["delay_next_seconds"]
        _state["fail_next"] = False
        _state["delay_next_seconds"] = 0.0
    if delay:
        time.sleep(delay)
    if should_fail:
        raise RuntimeError("injected DSH write failure")


app = create_dsh_app(os.environ["CHARACTER_MEMORY_CONFIG"], write_guard=_write_guard)


@app.get("/__e2e/dsh/runtime")
def runtime():
    return {"pid": os.getpid(), "model_loaded": False}


@app.post("/__e2e/dsh/fail-next-write")
def fail_next_write():
    with _lock:
        _state["fail_next"] = True
    return {"ok": True}


@app.post("/__e2e/dsh/delay-next-write")
def delay_next_write(seconds: float = 0.0):
    with _lock:
        _state["delay_next_seconds"] = max(0.0, float(seconds))
    return {"ok": True}
