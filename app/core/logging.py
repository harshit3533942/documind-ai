"""Logging configuration shared by the API and (later) Celery workers."""

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, with a consistent format."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )
    # Uvicorn's access log is noisy at INFO in dev; keep it but align level.
    logging.getLogger("uvicorn.access").setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
