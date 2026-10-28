"""Independent DSH listener.

Run this as its own process/port; it deliberately shares no FastAPI app with the
operator/Main API:

    uv run uvicorn character_memory.dsh_server:app --host 127.0.0.1 --port 8010

Only ``/health`` and the business routes under ``/v2/dsh/*`` are attached. Docs,
OpenAPI, static assets, ``/v1/*`` and ``/v2/activities`` do not exist here.
"""
from __future__ import annotations

import os

from character_memory.config import discover_character_profiles, load_settings
from character_memory.dsh.service import DshService
from character_memory.dsh.web import attach_dsh_business_routes
from character_memory.storage.sqlite import SQLiteStore
from character_memory.web_lifecycle import on_app_event


def create_dsh_app(config_path: str, *, write_guard=None, clock=None):
    from fastapi import FastAPI

    settings = load_settings(config_path)
    store = SQLiteStore(settings.db_path)
    profiles: dict[str, dict] = {}

    def profile_lookup(character_id: str) -> dict:
        if not profiles:
            for profile in discover_character_profiles(settings):
                profiles[str(profile["id"])] = profile
        return profiles.get(character_id, {"id": character_id, "name": character_id})

    service = DshService(
        store, clock=clock, write_guard=write_guard, profile_lookup=profile_lookup)
    app = FastAPI(
        title="character-memory-dsh",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.dsh_service = service
    attach_dsh_business_routes(app, service)

    @on_app_event(app, "shutdown")
    def _close_store():
        store.close()

    return app


config_path = os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml")
app = create_dsh_app(config_path)
