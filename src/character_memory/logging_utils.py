from __future__ import annotations

import logging
import os
import sys


_CONFIGURED = False


def configure_logging() -> logging.Logger:
    """Configure concise terminal logging for Character Memory once per process."""
    global _CONFIGURED
    logger = logging.getLogger("character_memory")
    level_name = os.getenv("CHARACTER_MEMORY_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logger.addHandler(handler)
        _CONFIGURED = True
    else:
        for handler in logger.handlers:
            handler.setLevel(level)

    return logger
