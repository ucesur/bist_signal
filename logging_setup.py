"""
logging_setup.py — Console + rotating file logger.
"""
import logging
import logging.handlers
import settings

_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def setup_logging() -> logging.Logger:
    console = logging.StreamHandler()
    console.setFormatter(_FMT)
    console.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))

    open(settings.LOG_FILE, "w").close()

    fh = logging.handlers.RotatingFileHandler(
        settings.LOG_FILE,
        maxBytes=settings.LOG_MAX_MB * 1024 * 1024,
        backupCount=settings.LOG_BACKUPS,
        encoding="utf-8",
    )
    fh.setFormatter(_FMT)
    fh.setLevel(logging.DEBUG)

    logging.basicConfig(level=logging.DEBUG, handlers=[console, fh])
    return logging.getLogger("bist_bot")
