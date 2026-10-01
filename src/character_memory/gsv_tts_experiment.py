"""Compatibility shim for the pre-formal GSV module name.

The production sidecar lives in :mod:`character_memory.gsv_tts_runtime`.
Keep this import path temporarily so existing local scripts and integrations do
not break on upgrade; new code must import/launch the runtime module directly.
"""

from character_memory.gsv_tts_runtime import *  # noqa: F401,F403
from character_memory.gsv_tts_runtime import _env_seed, main  # noqa: F401


if __name__ == "__main__":
    main()
