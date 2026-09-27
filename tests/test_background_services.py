from __future__ import annotations

from pathlib import Path
import threading

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient

from character_memory.background_services import BackgroundServices
from character_memory.server import create_server_app


# Registration order is start order, and shutdown runs it backwards. wake_loop
# must outrank async_reactions so the wake producer stops before the SSE hub it
# feeds is closed.
WORKERS_IN_START_ORDER = [
    "proactive_dispatch",
    "world_activity",
    "space_autonomy",
    "encounter_scheduler",
    "async_reactions",
    "group_autonomy",
    "wake_loop",
]


# The schedulers a feature module publishes on the runtime access object, so a
# test can observe that shutdown reaches each of them. A scheduler only starts
# when an API key is configured, which is why these tests set one.
SCHEDULER_HANDLES = (
    "world_activity_scheduler",
    "space_scheduler",
    "encounter_scheduler",
    "group_autonomy_scheduler",
)


def _config(tmp_path: Path, *, api_key: str = "") -> Path:
    personas = tmp_path / "personas"
    directory = personas / "c00"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "persona.yaml").write_text(
        yaml.safe_dump(
            {"id": "c00", "name": "角色00", "identity": "测试角色"},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f'api_key: "{api_key}"',
                # Nothing here may reach a real model endpoint, so the client
                # points at a closed local port: a worker that does run fails
                # fast instead of holding the thread through shutdown.
                'base_url: "http://127.0.0.1:9/v1"',
                'chat_model: "test"',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "lifecycle.db").as_posix()}"',
                f'media_dir: "{(tmp_path / "media").as_posix()}"',
                f'persona_path: "{(directory / "persona.yaml").as_posix()}"',
                # Every loop runs once on start and then waits, so the long
                # polls keep the workers idle rather than busy.
                "proactive_poll_seconds: 900",
                "encounter_poll_seconds: 900",
                "space_scheduler_poll_seconds: 900",
                "world_activity_poll_seconds: 900",
                "group_autonomy_poll_seconds: 900",
            ]
        ),
        encoding="utf-8",
    )
    return config


def _live_worker_threads() -> list[str]:
    """Worker threads still running, for diagnostics when an assertion fails."""

    current = threading.current_thread()
    return sorted(
        thread.name
        for thread in threading.enumerate()
        if thread is not current and thread.name.startswith("character-")
    )


def test_start_runs_in_registration_order_and_stop_runs_backwards():
    services = BackgroundServices()
    events: list[str] = []
    services.register(
        "producer",
        start=lambda: events.append("start:producer"),
        stop=lambda: events.append("stop:producer"),
    )
    services.register(
        "consumer",
        start=lambda: events.append("start:consumer"),
        stop=lambda: events.append("stop:consumer"),
    )

    services.start_all()
    services.stop_all()

    assert events == ["start:producer", "start:consumer", "stop:consumer", "stop:producer"]


def test_a_worker_may_own_only_its_stop():
    services = BackgroundServices()
    events: list[str] = []
    services.register("lazily_started", stop=lambda: events.append("stop"))

    services.start_all()
    assert events == []

    services.stop_all()
    assert events == ["stop"]


def test_one_failing_stop_does_not_skip_the_workers_behind_it():
    services = BackgroundServices()
    events: list[str] = []

    def failing_stop() -> None:
        events.append("stop:failing")
        raise RuntimeError("stop failed")

    services.register("failing", stop=failing_stop)
    services.register("healthy", stop=lambda: events.append("stop:healthy"))

    services.stop_all()

    assert events == ["stop:healthy", "stop:failing"]


def test_a_worker_that_cannot_start_does_not_leave_the_rest_silently_unstarted():
    services = BackgroundServices()
    started: list[str] = []

    def failing_start() -> None:
        raise RuntimeError("start failed")

    services.register("failing", start=failing_start, stop=lambda: None)
    services.register("later", start=lambda: started.append("later"), stop=lambda: None)

    with pytest.raises(RuntimeError):
        services.start_all()

    assert started == []


def test_an_empty_or_duplicate_registration_is_rejected():
    services = BackgroundServices()

    with pytest.raises(ValueError):
        services.register("nothing_to_do")

    services.register("once", stop=lambda: None)
    with pytest.raises(ValueError):
        services.register("once", stop=lambda: None)


def test_the_composed_runtime_registers_every_worker_in_one_place(tmp_path: Path):
    app = create_server_app(str(_config(tmp_path)))

    assert app.state.background_services.names() == WORKERS_IN_START_ORDER


def test_the_core_stops_every_worker_before_it_closes_shared_resources(tmp_path: Path):
    app = create_server_app(str(_config(tmp_path)))
    calls: list[str] = []

    registry = app.state.background_services
    stop_all = registry.stop_all

    def recording_stop_all() -> None:
        calls.append("workers_stopped")
        stop_all()

    registry.stop_all = recording_stop_all

    services = app.state.character_memory.services
    close = services.close

    def recording_close() -> None:
        calls.append("services_closed")
        close()

    services.close = recording_close

    with TestClient(app):
        pass

    assert calls == ["workers_stopped", "services_closed"]


def test_no_module_can_pick_its_own_position_in_the_shutdown_order(tmp_path: Path):
    app = create_server_app(str(_config(tmp_path)))

    handlers = [getattr(handler, "__qualname__", str(handler)) for handler in app.router.on_shutdown]

    assert handlers == ["create_api.<locals>._shutdown"]


def test_shutdown_stops_every_scheduler_the_composition_started(tmp_path: Path):
    app = create_server_app(str(_config(tmp_path, api_key="not-a-real-key")))
    access = app.state.character_memory
    stopped: list[str] = []

    for name in SCHEDULER_HANDLES:
        scheduler = getattr(access, name)
        stop = scheduler.stop

        def record(_name=name, _stop=stop) -> None:
            stopped.append(_name)
            _stop()

        scheduler.stop = record

    with TestClient(app):
        pass

    assert sorted(stopped) == sorted(SCHEDULER_HANDLES), _live_worker_threads()


def test_shutdown_closes_the_reaction_scheduler(tmp_path: Path):
    # ReactionScheduler ends through close(), so the core's former name list,
    # which probed for a .stop, could never reach it.
    app = create_server_app(str(_config(tmp_path)))
    scheduler = app.state.character_memory.reaction_scheduler
    closed: list[bool] = []

    close = scheduler.close

    def record() -> None:
        closed.append(True)
        close()

    scheduler.close = record

    with TestClient(app):
        pass

    assert closed == [True]
