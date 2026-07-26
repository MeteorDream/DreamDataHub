"""
logger.py - A general-purpose logging utility.

Usage:
    from logger import init_logging
    import logging

    init_logging()                              # defaults: level=DEBUG, dir="log"
    init_logging(level=logging.INFO)            # raise the global level
    init_logging(log_dir="my_logs")             # custom log directory
    init_logging(level=logging.WARNING,
                 log_dir="warn_only",
                 fmt="%(levelname)s: %(message)s")  # full customisation

Three per-level log files are written inside *log_dir*:
    debug.log  — DEBUG   and above  (everything; full trace)
    info.log   — INFO    and above   (normal operation, warnings & errors too)
    error.log  — ERROR   and above   (just errors — quick scan)

*level* is the root gate: records below it are dropped before reaching any
handler, so debug.log only receives DEBUG records when level <= DEBUG
(set log_level = "DEBUG" in config to populate it).

After calling init_logging() anywhere in the codebase, every module can log
via the standard library without any further setup:

    logger = logging.getLogger(__name__)
    logger.info("Hello, world!")
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# name -> minimum severity captured by that file
_LEVEL_FILES = (
    ("debug", logging.DEBUG),
    ("info", logging.INFO),
    ("error", logging.ERROR),
)
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_LOG_BACKUPS = 10


def init_logging(
    level: int = logging.DEBUG,
    log_dir: str = "log",
    fmt: str = "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
    datefmt: str = "%Y-%m-%d %H:%M:%S",
    file_mode: str = "a",
    encoding: str = "utf-8",
    console: bool = True,
) -> None:
    """Initialise the root logger with a console handler and per-level file handlers.

    This function is idempotent: calling it more than once replaces any
    previously attached console/file handlers rather than stacking duplicates.

    Args:
        level:     Minimum severity captured by the console handler and the
                   root logger (default: DEBUG). Records below *level* never
                   reach the files, so debug.log stays empty unless
                   level <= DEBUG.
        log_dir:   Directory holding debug.log / info.log / error.log
                   (default: "log").
        fmt:       Log-record format string shared by all handlers.
        datefmt:   Date/time format used inside the format string.
        file_mode: File open mode – "a" to append, "w" to overwrite each run.
        encoding:  Encoding used when writing the log files.
        console:   Whether to attach the stdout console handler (default: True).
    """
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)

    # --- Remove stale handlers of the same type to keep init idempotent ------
    root.handlers = [
        h for h in root.handlers if not isinstance(h, (logging.StreamHandler, logging.FileHandler))
    ]

    # --- Console handler (stdout) --------------------------------------------
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    # --- Per-level file handlers ---------------------------------------------
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    for name, lvl in _LEVEL_FILES:
        file_handler = RotatingFileHandler(
            log_path / f"{name}.log",
            mode=file_mode,
            maxBytes=MAX_LOG_SIZE,
            backupCount=MAX_LOG_BACKUPS,
            encoding=encoding,
        )
        file_handler.setLevel(lvl)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.info(
        "Logging initialised — level: %s | dir: %s",
        logging.getLevelName(level),
        log_path.resolve(),
    )
