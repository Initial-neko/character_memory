from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from character_memory.background_services import BackgroundServices


F = TypeVar("F", bound=Callable)


def on_app_event(app, event_type: str):
    """Register FastAPI/Starlette lifecycle callbacks without deprecated @app.on_event.

    Route modules are attached after app construction, so a single constructor
    lifespan cannot own every feature callback yet. Centralizing registration
    keeps the current composition model while removing FastAPI's deprecated
    decorator path.
    """

    def decorator(func: F) -> F:
        app.router.add_event_handler(event_type, func)
        return func

    return decorator


def background_services(app) -> BackgroundServices:
    """Return the composition's background-service registry.

    Fails fast when called before ``create_api()`` has composed the runtime, the
    way feature modules already assert ``app.state.character_memory``.
    """

    registry = getattr(app.state, "background_services", None)
    if registry is None:
        raise RuntimeError(
            "create_api() must expose app.state.background_services before feature routes are attached"
        )
    return registry
