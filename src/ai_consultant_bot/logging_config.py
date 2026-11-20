"""Central application logging configuration."""

import sys

from loguru import logger


def configure_logging(level: str = "INFO") -> None:
    """Send application logs to standard error."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )
