"""
logger.py - A general-purpose logging utility.

Usage:
    from logger import init_logging
    import logging

    init_logging()                              # defaults: level=DEBUG, file="app.log"
    init_logging(level=logging.INFO)            # change log level
    init_logging(log_file="my_app.log")         # custom log file
    init_logging(level=logging.WARNING,
                 log_file="warnings.log",
                 fmt="%(levelname)s: %(message)s")  # full customisation

After calling init_logging() anywhere in the codebase, every module can log
via the standard library without any further setup:

    logger = logging.getLogger(__name__)
    logger.info("Hello, world!")
"""

import logging
import sys
from pathlib import Path


def init_logging(
    level: int = logging.DEBUG,
    log_file: str = "app.log",
    fmt: str = "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
    datefmt: str = "%Y-%m-%d %H:%M:%S",
    file_mode: str = "a",
    encoding: str = "utf-8",
) -> None:
    """Initialise the root logger with a console handler and a file handler.

    This function is idempotent: calling it more than once replaces any
    previously attached console/file handlers rather than stacking duplicates.

    Args:
        level:     Minimum severity captured by both handlers (default: DEBUG).
        log_file:  Path to the log file (default: "app.log").
        fmt:       Log-record format string shared by both handlers.
        datefmt:   Date/time format used inside the format string.
        file_mode: File open mode – "a" to append, "w" to overwrite each run.
        encoding:  Encoding used when writing the log file.
    """
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)

    # --- Remove stale handlers of the same type to keep init idempotent ------
    root.handlers = [
        h for h in root.handlers if not isinstance(h, (logging.StreamHandler, logging.FileHandler))
    ]

    # --- Console handler (stdout) --------------------------------------------
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # --- File handler --------------------------------------------------------
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_path, mode=file_mode, encoding=encoding)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root.info(
        "Logging initialised — level: %s | file: %s",
        logging.getLevelName(level),
        log_path.resolve(),
    )
