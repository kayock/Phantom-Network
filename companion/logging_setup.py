"""Logging setup: rotating file log + concise console output.

Log files live in ``<data_dir>/logs/companion.log`` and are rotated at 2 MB
(5 backups kept).  Modules log via the standard ``logging`` module using
``logging.getLogger("pn.<module>")``.
"""

from __future__ import annotations

import logging
import logging.handlers
import os

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
_DATEFMT = "%m-%d %H:%M:%S"


def setup_logging(data_dir: str, level: str = "INFO", console: bool = True) -> str:
    log_dir = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "companion.log")

    root = logging.getLogger("pn")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        root.addHandler(ch)

    logging.captureWarnings(True)
    return log_path
