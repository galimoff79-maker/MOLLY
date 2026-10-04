from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Any

from config import (
    LOG_BACKUP_COUNT,
    LOG_DIR,
    LOG_LEVEL,
    LOG_MAX_BYTES,
)


# ============================================================
# Molly PTO — Logging
# ============================================================
#
# Логи пишутся в data/logs/:
#
#   molly.log     — логи приложения (старт, проект, индексация, ошибки)
#   uvicorn.log   — логи uvicorn (запросы, ошибки сервера)
#
# Файлы ротируются. Кодировка UTF-8 (важно для русских путей на Windows).


LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | "
    "%(name)s | %(message)s"
)

LOGGER_NAME = "molly"

_configured = False


def _level() -> int:

    level = logging.getLevelName(LOG_LEVEL)

    return level if isinstance(level, int) else logging.INFO


def setup_logging() -> logging.Logger:
    """
    Настраивает логгер "molly". Безопасно вызывать несколько раз
    (в том числе в процессе-потомке uvicorn --reload).
    """

    global _configured

    logger = logging.getLogger(LOGGER_NAME)

    if _configured:
        return logger

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.setLevel(_level())
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = RotatingFileHandler(
        LOG_DIR / "molly.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # В консоль — тоже, чтобы видеть ход работы при запуске из .bat.
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    _configured = True

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    get_logger(__name__) -> дочерний логгер molly.<name>.
    """

    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def uvicorn_log_config() -> dict[str, Any]:
    """
    log_config для uvicorn.run(): логи uvicorn идут и в консоль,
    и в data/logs/uvicorn.log.
    """

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": LOG_FORMAT,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "file": {
                "class": (
                    "logging.handlers.RotatingFileHandler"
                ),
                "formatter": "default",
                "filename": str(LOG_DIR / "uvicorn.log"),
                "maxBytes": LOG_MAX_BYTES,
                "backupCount": LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "delay": True,
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "level": "INFO",
            },
            "uvicorn.access": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }
