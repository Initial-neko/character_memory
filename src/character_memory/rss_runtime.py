from __future__ import annotations

from character_memory.rss_sources import RssRepository, RssScheduler, RssService
from character_memory.rss_web import attach_rss_routes


def attach_rss_runtime(
    app,
    web_dir,
    store,
    background,
    settings,
    *,
    own_bundle: bool,
) -> None:
    """Compose RSS collection without growing the core API composition root.

    RSS owns its repository, fetch service, scheduler and routes as one feature
    boundary. It uses the shared SQLite connection and BackgroundServices
    lifecycle, but it does not depend on CharacterRuntime or the model.
    """

    repository = RssRepository(store)
    service = RssService(repository)
    scheduler = RssScheduler(
        service,
        poll_seconds=float(getattr(settings, "rss_poll_seconds", 60.0)),
        enabled=bool(getattr(settings, "rss_enabled", True)) and own_bundle,
    )
    background.register("rss_sources", start=scheduler.start, stop=scheduler.stop)
    attach_rss_routes(
        app,
        web_dir,
        repository,
        service,
        default_interval_minutes=float(
            getattr(settings, "rss_default_fetch_interval_minutes", 60.0)
        ),
    )
