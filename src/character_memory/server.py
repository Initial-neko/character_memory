import os

from character_memory.api import create_api
from character_memory.async_web import attach_async_routes
from character_memory.avatar_web import attach_avatar_routes
from character_memory.encounter_web import attach_encounter_routes
from character_memory.ensemble_web import attach_ensemble_routes
from character_memory.group_autonomous_visual import install_group_autonomous_visual
from character_memory.group_autonomy_web import attach_group_autonomy_routes
from character_memory.group_members_web import attach_group_member_routes
from character_memory.group_web import attach_group_routes
from character_memory.history_web import attach_history_routes
from character_memory.memory_web import attach_memory_routes
from character_memory.search_web import attach_search_routes
from character_memory.space_web import attach_space_routes
from character_memory.visual_capture_web import attach_visual_capture_routes
from character_memory.visual_web import attach_visual_routes
from character_memory.voice_web import attach_voice_routes
from character_memory.wake_web import attach_wake_routes
from character_memory.world_web import attach_world_routes


config_path = os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml")
app = create_api(config_path)
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
attach_group_autonomy_routes(app)
install_group_autonomous_visual(app)
attach_visual_capture_routes(app)
attach_wake_routes(app)