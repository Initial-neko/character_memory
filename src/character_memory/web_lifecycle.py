from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar


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
