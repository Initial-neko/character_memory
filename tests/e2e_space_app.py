"""The deterministic browser app with Character Space routes attached."""

from e2e_realtime_app import app

from character_memory.space_web import attach_space_routes


attach_space_routes(app)
