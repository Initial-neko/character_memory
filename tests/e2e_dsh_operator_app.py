"""Operator/Main listener for real-HTTP DSH acceptance.

The acceptance suite only needs the Activity, Space and DSH-admin surfaces, so
this app composes ``create_api`` with exactly those routes. The full
``server.py`` composition is covered separately by the in-process production
composition test.
"""
from __future__ import annotations

import os

from character_memory.activity_web import attach_activity_routes
from character_memory.api import create_api
from character_memory.dsh.admin_web import attach_dsh_admin_routes
from character_memory.dsh.service import DshService
from character_memory.space_web import attach_space_routes

app = create_api(os.environ["CHARACTER_MEMORY_CONFIG"])
attach_activity_routes(app)
attach_space_routes(app)
attach_dsh_admin_routes(
    app, DshService(app.state.character_memory.read_store))
