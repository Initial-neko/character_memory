import os

from character_memory.api import create_api

app = create_api(os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml"))
