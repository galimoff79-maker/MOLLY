from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import index_runner
from database import (
    connect_db,
    get_index_status,
    rebuild_fts,
)
from logging_setup import get_logger
from project import (
    get_project_manager,
    require_current_project,
    set_current_project,
)
from search import (
    get_document,
    search_documents,
)

logger = get_logger("api")

router = APIRouter(
    prefix="/api",
    tags=["Molly PTO"],
)


# ============================================================
# Ошибки
# ============================================================

class ChooseProjectRequest(BaseModel):

    path: str = Field(
        ...,
        min_length=1,
        description="Путь к существующей папке проекта",
    )


SetProjectRequest = ChooseProjectRequest

_CLIENT_ERRORS = (
    ValueError,
    RuntimeError,
    FileNotFoundError,
    NotADirectoryError,
    PermissionError,
)


def http_error(message: str, status_code: int = 400, **extra: Any) -> HTTPException:

    return HTTPException(
        status_code=status_code,
        detail={"error": message, **extra},
    )


def fail(exc: Exception) -> HTTPException:

    if isinstance(exc, HTTPException):
        return exc

    if isinstance(exc, _CLIENT_ERRORS):
        return http_error(str(exc), 400)

    logger.exception("Необработанная ошибка в API")

    return http_error(f"Внутренняя ошибка: {exc}", 500)


# ============================================================
# Проект
# ============================================================

@router.get("/project")
async def api_project() -> dict[str, Any]:

    manager = get_project_manager()

    return await asyncio.to_thread(manager.get_project_info)


@router.post("/project/choose")
@router.post("/project/set", deprecated=True)
async def api_project_choose(
    request: ChooseProjectRequest,
) -> dict[str, Any]:

    if index_runner.is_running():
        raise http_error(
            "Идёт индексация. Остановите её и повторите.",
            409,
        )

    try:

        info = await asyncio.to_thread(set_current_project, request.path)

        return {"ok": True, "project": info}

    except Exception as exc:

        raise fail(exc)


# ============================================================
# Обзор папок (выбор рабочей папки в интерфейсе)
# ============================================================

def _list_dirs(path: str) -> dict[str, Any]:

    if not path:

        if sys.platform == "win32":

            drives = []

            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZAB":
                root = f"{letter}:\\"
                if os.path.exists(root):
                    drives.append({"name": root, "path": root})

            return {"path": "", "parent": None, "dirs": drives}

        path = "/"

    target = Path(path)

    if not target.is_dir():
        raise FileNotFoundError(f"Папка не найдена: {path}")

    dirs = []

    try:

        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(("$", ".")):
                        dirs.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue

    except PermissionError:
        raise PermissionError(f"Нет доступа к папке: {path}")

    dirs.sort(key=lambda d: d["name"].lower())

    parent = str(target.parent) if target.parent != target else ""

    return {"path": str(target), "parent": parent, "dirs": dirs[:2000]}


@router.get("/fs/list")
async def api_fs_list(path: str = Query("")) -> dict[str, Any]:

    try:
        return await asyncio.to_thread(_list_dirs, path)
    except Exception as exc:
        raise fail(exc)


# ============================================================
# Индекс
# ============================================================

def _count_documents(project: Path) -> int:

    with connect_db(project) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])


@router.get("/index/status")
async def api_index_status() -> dict[str, Any]:

    try:

        project = await asyncio.to_thread(require_current_project)

        running = index_runner.is_running()

        status = index_runner.snapshot(project) if running else None

        if status is None:

            status = await asyncio.to_thread(get_index_status, project)

            if not running and status.get("status") == "indexing":
                status["status"] = "interrupted"

        documents = await asyncio.to_thread(_count_documents, project)

        return {
            "ok": True,
            "project": str(project),
            "running": running,
            "documents": documents,
            "status": status,
        }

    except Exception as exc:

        raise fail(exc)


@router.post("/index")
async def api_index() -> dict[str, Any]:

    try:
        project = await asyncio.to_thread(require_current_project)
    except Exception as exc:
        raise fail(exc)

    started = index_runner.start(project, "manual")

    if not started:
        return {
            "ok": True,
            "started": False,
            "message": "Индексация уже выполняется.",
        }

    return {"ok": True, "started": True, "project": str(project)}


@router.post("/index/stop")
async def api_index_stop() -> dict[str, Any]:

    stopped = index_runner.stop()

    return {
        "ok": True,
        "stopped": stopped,
        **({} if stopped else {"message": "Индексация сейчас не выполняется."}),
    }


@router.post("/index/rebuild-fts")
async def api_index_rebuild_fts() -> dict[str, Any]:

    if index_runner.is_running():
        raise http_error("Идёт индексация — дождитесь завершения.", 409)

    try:
        project = await asyncio.to_thread(require_current_project)
        count = await asyncio.to_thread(rebuild_fts, project)
        return {"ok": True, "documents": count}
    except Exception as exc:
        raise fail(exc)


# ============================================================
# Поиск и документы
# ============================================================

@router.get("/search")
async def api_search(
    q: str = Query(..., min_length=1, description="Поисковый запрос"),
    limit: int = Query(10, ge=1, le=100),
    section: str | None = Query(None),
    document_type: str | None = Query(None),
) -> dict[str, Any]:

    try:

        project = await asyncio.to_thread(require_current_project)

        results = await asyncio.to_thread(
            search_documents,
            project_path=project,
            query=q,
            limit=limit,
            section=section,
            document_type=document_type,
        )

        return {
            "ok": True,
            "query": q,
            "count": len(results),
            "results": results,
        }

    except Exception as exc:

        raise fail(exc)


@router.get("/file/{document_id}")
async def api_file(document_id: int) -> dict[str, Any]:

    try:

        project = await asyncio.to_thread(require_current_project)

        result = await asyncio.to_thread(get_document, project, document_id)

        if result is None:
            raise http_error("Документ не найден.", 404)

        return {"ok": True, "document": result}

    except Exception as exc:

        raise fail(exc)
