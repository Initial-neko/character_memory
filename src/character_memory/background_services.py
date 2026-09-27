from __future__ import annotations

from collections.abc import Callable
import logging


logger = logging.getLogger("character_memory.background_services")


class BackgroundServices:
    """Owns the start and stop order of the process-local background workers.

    Starlette runs shutdown handlers in registration order, so a module that
    needed a different position could only express it by inserting itself into
    ``app.router.on_shutdown``. Two modules do that today, and the order they
    produce still lets one worker outlive the shared store that the core closes
    afterwards. Workers register here instead, so the position comes from one
    list rather than from a mix of attach order and front-insertion.

    Stop order is the reverse of start order, so a producer registered after
    the worker it feeds stops before it. Callers must run ``stop_all()`` before
    closing any resource a worker can touch; that is the caller's contract, not
    this class's.
    """

    def __init__(self) -> None:
        self._workers: list[
            tuple[str, Callable[[], None] | None, Callable[[], None] | None]
        ] = []

    def register(
        self,
        name: str,
        *,
        start: Callable[[], None] | None = None,
        stop: Callable[[], None] | None = None,
    ) -> None:
        """Register one worker.

        ``start`` is optional because a scheduler may bring its thread up
        lazily, and ``stop`` is optional for a worker that ends on its own.
        Registering neither is a mistake rather than an empty registration, and
        two workers may not share a name.
        """

        if start is None and stop is None:
            raise ValueError(f"background worker {name!r} registers neither start nor stop")
        if name in self.names():
            raise ValueError(f"background worker {name!r} is already registered")
        self._workers.append((name, start, stop))

    def names(self) -> list[str]:
        """Registered worker names, in start order."""

        return [name for name, _, _ in self._workers]

    def start_all(self) -> None:
        for name, start, _ in self._workers:
            if start is None:
                continue
            try:
                start()
            except Exception:
                # A worker that cannot start leaves the runtime half-composed,
                # so surface it instead of continuing to the next one.
                logger.exception("background_services start_failed worker=%s", name)
                raise

    def stop_all(self) -> None:
        """Stop every worker, newest first, without letting one failure skip the rest."""

        for name, _, stop in reversed(self._workers):
            if stop is None:
                continue
            try:
                stop()
            except Exception:
                logger.exception("background_services stop_failed worker=%s", name)
