from __future__ import annotations

import asyncio
import os
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import index_runner
import lan
import settings_store
from api import router as project_router
from api_chat import router as chat_router
from api_mail import router as mail_router
from api_system import router as system_router
from config import (
    APP_HOST,
    APP_PORT,
    APP_RELOAD,
    BASE_DIR,
    LOG_DIR,
    ensure_directories,
)
from database import (
    initialize_database,
    mark_stale_indexing,
    project_db_path,
)
from logging_setup import (
    setup_logging,
    uvicorn_log_config,
)
from project import get_project_manager

# ============================================================
# МОЛЛИ — AI-помощник ПТО
# ============================================================

ensure_directories()

logger = setup_logging()

APP_VERSION = "1.0.0"

FRONTEND_DIR = BASE_DIR / "frontend"

_scheduler_stop = threading.Event()


# ============================================================
# Lifespan
# ============================================================

def _startup_sync() -> None:

    manager = get_project_manager()

    project = manager.project_path

    if project is None:
        logger.info("Рабочая папка не выбрана")
    else:

        logger.info("Рабочая папка: %s", project)

        try:
            initialize_database(project)

            if mark_stale_indexing(project):
                logger.warning(
                    "Прошлая индексация была прервана — статус сброшен"
                )
        except Exception:
            logger.exception("Не удалось подготовить базу рабочей папки")

    # Сервер в локальной сети — если он был включён при прошлом выходе.
    lan_settings = settings_store.get()["lan"]

    if lan_settings["enabled"] and lan_settings["token"]:
        try:
            lan.lan_server.start(app, lan_settings["port"])
        except lan.LanError as exc:
            logger.warning("LAN-сервер не запущен автоматически: %s", exc)
            settings_store.update({"lan": {"enabled": False}})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:

    logger.info("МОЛЛИ %s запускается", APP_VERSION)

    await asyncio.to_thread(_startup_sync)

    _scheduler_stop.clear()

    threading.Thread(
        target=index_runner.scheduler_loop,
        args=(lambda: get_project_manager().project_path, _scheduler_stop),
        name="molly-scheduler",
        daemon=True,
    ).start()

    if os.getenv("MOLLY_OPEN_BROWSER", "0") == "1":
        threading.Timer(
            1.0,
            webbrowser.open,
            args=(f"http://127.0.0.1:{APP_PORT}",),
        ).start()

    yield

    logger.info("МОЛЛИ останавливается")

    _scheduler_stop.set()

    await asyncio.to_thread(lan.lan_server.stop)

    await asyncio.to_thread(index_runner.shutdown)

    logger.info("МОЛЛИ остановлена")


# ============================================================
# Приложение
# ============================================================

app = FastAPI(
    title="МОЛЛИ — AI-помощник ПТО",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(project_router)
app.include_router(chat_router)
app.include_router(mail_router)
app.include_router(system_router)

# CORS не нужен: интерфейс отдаётся с того же адреса.
# Доступ из сети и защита от запросов с чужих сайтов — в AccessMiddleware.
app.add_middleware(lan.AccessMiddleware, lan_server=lan.lan_server)

if (FRONTEND_DIR / "static").is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=str(FRONTEND_DIR / "static")),
        name="static",
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        FRONTEND_DIR / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": APP_VERSION})


# ============================================================
# Прямой запуск
# ============================================================
#
#   python main.py
#   MOLLY_PORT=8010 python main.py
#   MOLLY_RELOAD=1 python main.py    (разработка)

def _print_banner() -> None:

    project = get_project_manager().project_path

    shown = "127.0.0.1" if APP_HOST in ("0.0.0.0", "") else APP_HOST

    print(
        "\n".join(
            [
                "",
                "=" * 60,
                f"  МОЛЛИ — AI-помощник ПТО  v{APP_VERSION}",
                "=" * 60,
                f"  Откройте в браузере: http://{shown}:{APP_PORT}",
                f"  Рабочая папка:       {project or 'не выбрана'}",
                f"  База:                {project_db_path(project) if project else '—'}",
                f"  Журнал:              {LOG_DIR}",
                "",
                "  Чтобы закрыть МОЛЛИ, закройте это окно.",
                "=" * 60,
                "",
            ]
        ),
        flush=True,
    )


if __name__ == "__main__":

    import uvicorn

    if lan.port_in_use(APP_HOST, APP_PORT):

        message = (
            f"Порт {APP_PORT} уже занят (возможно, МОЛЛИ уже запущена — "
            f"откройте http://127.0.0.1:{APP_PORT}). Чтобы использовать "
            "другой порт, задайте переменную MOLLY_PORT."
        )

        logger.error(message)

        print(f"ОШИБКА: {message}", file=sys.stderr)

        sys.exit(1)

    _print_banner()

    uvicorn.run(
        "main:app",
        host=APP_HOST,
        port=APP_PORT,
        reload=APP_RELOAD,
        reload_dirs=([str(Path(__file__).resolve().parent)] if APP_RELOAD else None),
        log_config=uvicorn_log_config(),
    )
