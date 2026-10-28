import os

from character_memory.call_invitation_web import attach_call_invitation_routes
from character_memory.activity_web import attach_activity_routes
from character_memory.api import create_api
from character_memory.async_web import attach_async_routes
from character_memory.avatar_web import attach_avatar_routes
from character_memory.encounter_web import attach_encounter_routes
from character_memory.ensemble_web import attach_ensemble_routes
from character_memory.group_autonomous_visual import install_group_autonomous_visual
from character_memory.group_autonomy_web import attach_group_autonomy_routes
from character_memory.group_members_web import attach_group_member_routes
from character_memory.group_web import attach_group_routes
from character_memory.dsh.admin_web import attach_dsh_admin_routes
from character_memory.dsh.service import DshService
from character_memory.history_web import attach_history_routes
from character_memory.memory_web import attach_memory_routes
from character_memory.reflection_runtime import attach_reflection_runtime
from character_memory.search_web import attach_search_routes
from character_memory.scenario_web import attach_scenario_routes
from character_memory.space_web import attach_space_routes
from character_memory.visual_capture_web import attach_visual_capture_routes
from character_memory.visual_web import attach_visual_routes
from character_memory.voice_web import attach_voice_routes
from character_memory.wake_web import attach_wake_routes
from character_memory.world_web import attach_world_routes


def create_server_app(config_path: str):
    """Compose Character Runtime: the core API plus every feature route.

    A function rather than module-level statements so the composition can be
    built against a temporary config, which is how the background-worker
    lifecycle is verified.
    """

    app = create_api(config_path)
    # Local-only lifecycle state; attaching it never warms up the model bundle.
    attach_activity_routes(app)
    # DSH admin lives only on this operator listener. The business surface is a
    # separate app/port (`dsh_server.py`) so a DSH client cannot reach Main API
    # routes by construction.
    attach_dsh_admin_routes(
        app, DshService(app.state.character_memory.read_store))
    # Practice routes read the same Activity rows; only an explicit turn calls
    # the model, and every fact they write carries the frozen SIMULATED scope.
    attach_scenario_routes(app)
    attach_history_routes(app)
    attach_memory_routes(app)
    # RuntimeServices are composed before any feature route attaches. World no
    # longer depends on Avatar route initialization order.
    attach_world_routes(app)
    attach_avatar_routes(app)
    attach_voice_routes(app)
    attach_visual_routes(app)
    attach_group_routes(app, config_path)
    attach_group_member_routes(app)
    attach_ensemble_routes(app)
    attach_search_routes(app)
    attach_space_routes(app)
    attach_encounter_routes(app)
    attach_async_routes(app)
    attach_call_invitation_routes(app)
    attach_group_autonomy_routes(app)
    install_group_autonomous_visual(app)
    attach_visual_capture_routes(app)
    attach_wake_routes(app)
    # Reflection registers last: it is a read-mostly ledger worker, and being
    # newest means it stops first on shutdown, before any store it reads closes.
    attach_reflection_runtime(
        app,
        app.state.character_memory.read_store,
        app.state.background_services,
        app.state.character_memory.settings,
        own_bundle=True,
    )
    return app


config_path = os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml")
app = create_server_app(config_path)