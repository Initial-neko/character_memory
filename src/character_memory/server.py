import os

from character_memory.api import create_api
from character_memory.group_web import attach_group_routes


config_path = os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml")
app = create_api(config_path)
attach_group_routes(app, config_path)
