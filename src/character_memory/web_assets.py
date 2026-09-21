"""The one way to serve the pages and their assets.

Every page here is edited in place by whoever is working on it, and none of the
mounts sent a ``Cache-Control``. Without one a browser falls back to heuristic
freshness -- a fraction of the file's age -- and will happily run a bundle that
is hours old, which is how a fixed frontend ends up looking unfixed.

``no-cache`` is the right value rather than ``no-store``: the browser keeps the
file and revalidates it against the ETag ``StaticFiles`` already sends, so a
reload costs a 304 and the running page is always the one on disk.
"""

from __future__ import annotations

from pathlib import Path


# Content types that describe the UI itself. Audio, media and the JSON API are
# left alone: they are answers, not the program that asks the questions.
ASSET_CONTENT_TYPES = ("text/html", "text/css", "javascript")


def attach_static_assets(app, web_dir: str | Path, *, name: str = "static") -> None:
    """Mount ``/static`` and stop the UI from being served out of a stale cache."""

    from fastapi.staticfiles import StaticFiles

    @app.middleware("http")
    async def _revalidate_web_assets(request, call_next):
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if any(token in content_type for token in ASSET_CONTENT_TYPES):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=str(web_dir)), name=name)
