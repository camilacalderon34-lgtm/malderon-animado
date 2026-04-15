"""
Centralized logging configuration for Malderon Creator.

Usage in any module:
    from app.logger import get_logger
    logger = get_logger(__name__)
    logger.info("something happened")
    logger.error("something failed", exc_info=True)
"""
import logging
import sys


def get_logger(name: str) -> logging.Logger:
    """Return a logger that writes UTF-8 to stdout with a consistent format."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "[%(levelname)s][%(name)s] %(message)s"
        ))
        # Force UTF-8 encoding on Windows
        if hasattr(handler.stream, "reconfigure"):
            try:
                handler.stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    return logger
