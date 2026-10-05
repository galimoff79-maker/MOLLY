from __future__ import annotations

import os
from pathlib import Path


# ============================================================
# Molly PTO — Configuration
# ============================================================


# ------------------------------------------------------------
# Paths
# ------------------------------------------------------------

BASE_DIR = Path(
    __file__
).resolve().parent.parent

BACKEND_DIR = (
    BASE_DIR / "backend"
)

DATA_DIR = (
    BASE_DIR / "data"
)

PROJECTS_DATA_DIR = (
    DATA_DIR / "projects"
)

PROJECT_CONFIG_FILE = (
    DATA_DIR / "project.json"
)

LOG_DIR = (
    DATA_DIR / "logs"
)


# ------------------------------------------------------------
# Default project
# ------------------------------------------------------------

# Папка проекта по умолчанию берётся из переменной окружения
# MOLLY_PROJECT_DIR. Если переменная не задана — None:
# тогда используется путь, сохранённый в data/project.json,
# либо проект выбирается через API.

_default_project_env = os.getenv(
    "MOLLY_PROJECT_DIR",
    "",
).strip().strip('"')

DEFAULT_PROJECT_DIR: Path | None = (
    Path(_default_project_env)
    if _default_project_env
    else None
)


# ------------------------------------------------------------
# FastAPI
# ------------------------------------------------------------

APP_HOST = os.getenv(
    "MOLLY_HOST",
    "127.0.0.1",
)

APP_PORT = int(
    os.getenv(
        "MOLLY_PORT",
        "8000",
    )
)

# MOLLY_RELOAD=1 — автоперезапуск при изменении .py (для разработки).
APP_RELOAD = os.getenv(
    "MOLLY_RELOAD",
    "0",
).strip().lower() in {"1", "true", "yes", "on"}


# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------

LOG_LEVEL = os.getenv(
    "MOLLY_LOG_LEVEL",
    "INFO",
).strip().upper()

LOG_MAX_BYTES = int(
    os.getenv(
        "MOLLY_LOG_MAX_BYTES",
        str(5 * 1024 * 1024),
    )
)

LOG_BACKUP_COUNT = int(
    os.getenv(
        "MOLLY_LOG_BACKUP_COUNT",
        "5",
    )
)


# ------------------------------------------------------------
# Ollama
# ------------------------------------------------------------

OLLAMA_BASE_URL = os.getenv(
    "OLLAMA_BASE_URL",
    "http://127.0.0.1:11434",
).rstrip("/")


OLLAMA_DEFAULT_MODEL = os.getenv(
    "OLLAMA_DEFAULT_MODEL",
    "molly-pto:latest",
)


# ------------------------------------------------------------
# FreeLLMAPI (онлайн-провайдер OpenAI-compatible)
# ------------------------------------------------------------

FREELLMAPI_BASE_URL = os.getenv(
    "FREELLMAPI_BASE_URL",
    "http://127.0.0.1:31415",
).rstrip("/")

# API-ключ берётся только из окружения / защищённого хранилища
# (secrets_store), никогда не хранится в коде и настройках.
FREELLMAPI_API_KEY_ENV = "FREELLMAPI_API_KEY"

# Лимит тела запроса для онлайн-API (128 КБ, как у FreeLLMAPI),
# с запасом на JSON-экранирование и системный промпт.
FREELLMAPI_MAX_REQUEST_BYTES = int(
    os.getenv(
        "MOLLY_FREELLMAPI_MAX_REQUEST_BYTES",
        str(120_000),
    )
)


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

HTTP_TIMEOUT = float(
    os.getenv(
        "MOLLY_HTTP_TIMEOUT",
        "120",
    )
)


# Очень важно для Windows:
#
# Если в системе задан HTTP(S)_PROXY,
# httpx может попытаться отправить
# localhost-запрос через прокси.
#
# Для FreeLLMAPI / Ollama это нам не нужно.

HTTP_TRUST_ENV = False


# ------------------------------------------------------------
# Indexing
# ------------------------------------------------------------

# Как часто сбрасывать прогресс индексации в SQLite:
# раз в INDEX_BATCH_SIZE файлов ИЛИ раз в INDEX_PROGRESS_INTERVAL секунд.
# (Индексация идёт в один поток, поэтому INDEX_WORKERS убран:
# SQLite всё равно допускает одного писателя.)

INDEX_BATCH_SIZE = max(
    1,
    int(
        os.getenv(
            "MOLLY_INDEX_BATCH_SIZE",
            "25",
        )
    ),
)

INDEX_PROGRESS_INTERVAL = float(
    os.getenv(
        "MOLLY_INDEX_PROGRESS_INTERVAL",
        "1.0",
    )
)


# Максимальный объём текста одного
# документа, который сохраняем в БД.
#
# Это защищает RAM от огромных
# Excel/PDF/TXT файлов.

MAX_DOCUMENT_TEXT = int(
    os.getenv(
        "MOLLY_MAX_DOCUMENT_TEXT",
        str(2_000_000),
    )
)


# ------------------------------------------------------------
# Search
# ------------------------------------------------------------

SEARCH_DEFAULT_LIMIT = int(
    os.getenv(
        "MOLLY_SEARCH_DEFAULT_LIMIT",
        "10",
    )
)


SEARCH_MAX_LIMIT = int(
    os.getenv(
        "MOLLY_SEARCH_MAX_LIMIT",
        "100",
    )
)


# ------------------------------------------------------------
# RAG
# ------------------------------------------------------------

RAG_DEFAULT_LIMIT = int(
    os.getenv(
        "MOLLY_RAG_DEFAULT_LIMIT",
        "10",
    )
)


RAG_MAX_LIMIT = int(
    os.getenv(
        "MOLLY_RAG_MAX_LIMIT",
        "15",
    )
)


RAG_SNIPPET_LENGTH = int(
    os.getenv(
        "MOLLY_RAG_SNIPPET_LENGTH",
        "4000",
    )
)


# ------------------------------------------------------------
# Supported files
# ------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",

    ".xls",
    ".xlsx",
    ".xlsm",

    ".xml",

    ".txt",
    ".csv",
    ".json",

    ".dwg",
}


# ------------------------------------------------------------
# Ignored extensions
# ------------------------------------------------------------

IGNORED_EXTENSIONS = {
    ".bak",
    ".tmp",
    ".temp",

    ".dwl",
    ".dwl2",

    ".lock",
    ".lck",

    ".swp",
    ".swo",

    ".cache",

    ".log",
}


# ------------------------------------------------------------
# Ignored filenames
# ------------------------------------------------------------

IGNORED_FILENAMES = {
    "thumbs.db",
    "desktop.ini",

    ".ds_store",

    "ntuser.dat",
    "ntuser.dat.log",
}


# ------------------------------------------------------------
# Ignored directories
# ------------------------------------------------------------

IGNORED_DIRECTORIES = {
    ".git",
    ".svn",
    ".hg",

    "__pycache__",

    "node_modules",

    "$recycle.bin",

    "system volume information",
}


# ------------------------------------------------------------
# SQLite
# ------------------------------------------------------------

SQLITE_TIMEOUT = float(
    os.getenv(
        "MOLLY_SQLITE_TIMEOUT",
        "30",
    )
)


# Повторы при SQLITE_BUSY / "database is locked" на операциях записи.

SQLITE_BUSY_RETRIES = int(
    os.getenv(
        "MOLLY_SQLITE_BUSY_RETRIES",
        "5",
    )
)

SQLITE_BUSY_BACKOFF = float(
    os.getenv(
        "MOLLY_SQLITE_BUSY_BACKOFF",
        "0.2",
    )
)


SQLITE_PRAGMAS = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "foreign_keys": "ON",
    "temp_store": "MEMORY",
}


# ------------------------------------------------------------
# Directory initialization
# ------------------------------------------------------------

def ensure_directories() -> None:

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    PROJECTS_DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )