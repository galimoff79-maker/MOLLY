from __future__ import annotations

import asyncio
import importlib
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import lan
import llm
import secrets_store
import settings_store
from api import http_error
from api_chat import ctx
from config import APP_PORT, DATA_DIR, LOG_DIR
from database import connect_db, project_db_path
from logging_setup import get_logger
from project import get_project_manager

logger = get_logger("api_system")

router = APIRouter(prefix="/api", tags=["Система"])


# ============================================================
# Вход (для пользователей LAN)
# ============================================================

class LoginBody(BaseModel):
    token: str = Field(..., max_length=64)
    name: str = Field(..., max_length=60)


@router.get("/auth/status")
async def api_auth_status(request: Request) -> dict[str, Any]:

    c = ctx(request)

    local = bool(c.get("is_local"))

    return {
        "is_local": local,
        "authenticated": local or bool(c.get("owner")),
        "name": c.get("name", ""),
        "lan_running": lan.lan_server.running,
    }


@router.post("/auth/login")
async def api_auth_login(body: LoginBody, request: Request) -> JSONResponse:

    c = ctx(request)

    if c.get("is_local"):
        return JSONResponse({"ok": True, "name": body.name})

    ip = c.get("ip") or ""

    agent = request.headers.get("user-agent", "")

    try:
        sid, name = await asyncio.to_thread(lan.login, body.token, body.name, ip, agent)
    except lan.LanError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)

    response = JSONResponse({"ok": True, "name": name})

    response.set_cookie(
        lan.SESSION_COOKIE,
        sid,
        max_age=lan.SESSION_TTL,
        httponly=True,
        samesite="strict",
        path="/",
    )

    return response


@router.post("/auth/logout")
async def api_auth_logout(request: Request) -> JSONResponse:

    lan.logout(ctx(request).get("sid"))

    response = JSONResponse({"ok": True})

    response.delete_cookie(lan.SESSION_COOKIE, path="/")

    return response


# ============================================================
# Сервер в локальной сети (только владелец, см. lan.lan_allowed)
# ============================================================

class LanStartBody(BaseModel):
    port: int | None = Field(None, ge=1024, le=65535)


class KickBody(BaseModel):
    id: str = Field(..., min_length=4, max_length=16)


def _lan_state() -> dict[str, Any]:

    s = settings_store.get()["lan"]

    port = lan.lan_server.port if lan.lan_server.running else s["port"]

    ips = lan.lan_ips()

    return {
        "running": lan.lan_server.running,
        "enabled": s["enabled"],
        "port": port,
        "token": s["token"],
        "ips": ips,
        "urls": [f"http://{ip}:{port}" for ip in ips],
        "clients": lan.list_clients(),
        "error": lan.lan_server.error,
        "local_port": APP_PORT,
    }


@router.get("/lan")
async def api_lan() -> dict[str, Any]:

    return await asyncio.to_thread(_lan_state)


def _lan_start(app: Any, port: int | None) -> dict[str, Any]:

    s = settings_store.get()["lan"]

    port = port or s["port"]

    if port == APP_PORT:
        raise http_error(
            f"Порт {port} уже используется основным окном МОЛЛИ. Выберите другой.",
            400,
        )

    patch: dict[str, Any] = {"port": port, "enabled": True}

    if not s["token"]:
        patch["token"] = settings_store.new_lan_token()

    settings_store.update({"lan": patch})

    try:
        lan.lan_server.start(app, port)
    except lan.LanError as exc:
        settings_store.update({"lan": {"enabled": False}})
        raise http_error(str(exc), 400)

    return _lan_state()


@router.post("/lan/start")
async def api_lan_start(body: LanStartBody, request: Request) -> dict[str, Any]:

    return await asyncio.to_thread(_lan_start, request.app, body.port)


@router.post("/lan/stop")
async def api_lan_stop() -> dict[str, Any]:

    def work() -> dict[str, Any]:
        lan.lan_server.stop()
        settings_store.update({"lan": {"enabled": False}})
        return _lan_state()

    return await asyncio.to_thread(work)


@router.post("/lan/token")
async def api_lan_token() -> dict[str, Any]:
    """Новый токен; все текущие сессии пользователей LAN завершаются."""

    def work() -> dict[str, Any]:
        settings_store.update({"lan": {"token": settings_store.new_lan_token()}})
        lan.revoke_all()
        return _lan_state()

    return await asyncio.to_thread(work)


@router.post("/lan/kick")
async def api_lan_kick(body: KickBody) -> dict[str, Any]:

    lan.kick(body.id)

    return {"ok": True, "clients": lan.list_clients()}


# ============================================================
# Диагностика и журнал
# ============================================================

def _check(name: str, ok: bool, detail: str = "", hint: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "hint": hint}


def _diagnostics() -> dict[str, Any]:

    checks: list[dict[str, Any]] = []

    checks.append(
        _check(
            "Python",
            sys.version_info >= (3, 10),
            f"{platform.python_version()} ({platform.system()} {platform.release()})",
            "Нужен Python 3.10 или новее.",
        )
    )

    for module, title, purpose in (
        ("fastapi", "fastapi", "веб-сервер"),
        ("uvicorn", "uvicorn", "веб-сервер"),
        ("pypdf", "pypdf", "чтение PDF"),
        ("docx", "python-docx", "чтение DOCX"),
        ("openpyxl", "openpyxl", "чтение XLSX/XLSM"),
        ("xlrd", "xlrd", "чтение XLS"),
        ("olefile", "olefile", "чтение DOC"),
    ):
        try:
            importlib.import_module(module)
            checks.append(_check(f"Пакет {title}", True, purpose))
        except ImportError:
            checks.append(
                _check(
                    f"Пакет {title}",
                    False,
                    f"не установлен ({purpose})",
                    "Закройте МОЛЛИ, удалите папку .venv и запустите start.bat заново.",
                )
            )

    # Папка данных доступна для записи?
    try:
        probe = DATA_DIR / ".write_test"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(_check("Папка данных", True, str(DATA_DIR)))
    except OSError as exc:
        checks.append(
            _check(
                "Папка данных",
                False,
                f"{DATA_DIR}: {exc}",
                "Распакуйте МОЛЛИ в папку, где у вас есть право записи "
                "(не в Program Files).",
            )
        )

    free = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else Path.home()).free

    checks.append(
        _check(
            "Свободное место",
            free > 500 * 1024 * 1024,
            f"{free // (1024 * 1024)} МБ",
            "Индекс больших проектов занимает сотни мегабайт.",
        )
    )

    s = settings_store.get()

    ollama = llm.check(s["ollama_url"], s["model"])

    if ollama["ok"]:
        checks.append(
            _check("Ollama", True, f"версия {ollama['version']}, моделей: {len(ollama['models'])}")
        )
        checks.append(
            _check(
                f"Модель «{s['model']}»",
                bool(ollama["model_present"]),
                "найдена" if ollama["model_present"] else "не найдена в Ollama",
                "Выберите другую модель в Настройки → Модель.",
            )
        )
    else:
        checks.append(_check("Ollama", False, ollama["error"] or "недоступна"))

    project = get_project_manager().project_path

    if project is None:
        checks.append(_check("Рабочая папка", False, "не выбрана", "Настройки → Документы."))
    else:
        exists = Path(project).exists()
        checks.append(
            _check(
                "Рабочая папка",
                exists,
                str(project),
                "" if exists else "Папка недоступна (отключён диск/сеть?).",
            )
        )

        if exists:
            try:
                with connect_db(project) as conn:
                    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                    errors = conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE parser_status = 'error'"
                    ).fetchone()[0]
                checks.append(
                    _check(
                        "Индекс документов",
                        count > 0,
                        f"{count} документов, из них с ошибкой чтения: {errors}",
                        "Запустите индексацию: Настройки → Документы." if count == 0 else "",
                    )
                )
            except Exception as exc:
                checks.append(_check("Индекс документов", False, str(exc)))

    checks.append(
        _check(
            "Хранилище паролей",
            secrets_store.is_available(),
            "Windows DPAPI" if secrets_store.is_available() else "недоступно",
            "Почта работает только в Windows.",
        )
    )

    checks.append(
        _check(
            "Сервер в локальной сети",
            True,
            "включён" if lan.lan_server.running else "выключен",
        )
    )

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "log_dir": str(LOG_DIR),
        "data_dir": str(DATA_DIR),
    }


@router.get("/diagnostics")
async def api_diagnostics() -> dict[str, Any]:

    return await asyncio.to_thread(_diagnostics)


@router.get("/logs")
async def api_logs(lines: int = 300) -> dict[str, Any]:

    lines = max(10, min(lines, 2000))

    path = LOG_DIR / "molly.log"

    def work() -> str:
        if not path.exists():
            return ""
        data = path.read_bytes()[-400_000:]
        return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])

    return {"path": str(path), "text": await asyncio.to_thread(work)}
