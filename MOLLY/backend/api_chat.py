from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

import chats_store
import llm
import rag
import settings_store
from api import fail, http_error
from logging_setup import get_logger
from project import get_project_manager

logger = get_logger("api_chat")

router = APIRouter(prefix="/api", tags=["МОЛЛИ"])


def ctx(request: Request) -> dict[str, Any]:
    """Кто делает запрос (его определяет AccessMiddleware)."""

    return request.scope.get("molly") or {
        "is_local": True,
        "owner": "local",
        "name": "",
    }


def owner_of(request: Request) -> str:

    owner = ctx(request).get("owner")

    if not owner:
        raise http_error("Требуется вход.", 401, login_required=True)

    return owner


# ============================================================
# Настройки
# ============================================================

class SettingsBody(BaseModel):
    data: dict[str, Any]


class BackupBody(BaseModel):
    name: str


@router.get("/ui-config")
async def api_ui_config(request: Request) -> dict[str, Any]:
    """Минимум настроек, нужный интерфейсу (доступен и пользователям LAN)."""

    s = await asyncio.to_thread(settings_store.get)

    c = ctx(request)

    return {
        "is_local": bool(c.get("is_local")),
        "user": c.get("name") or s["user_name"],
        "assistant_name": s["assistant_name"],
        "theme": s["theme"],
        "model": s["model"],
        "use_documents": s["use_documents"],
        "setup_done": s["setup_done"],
    }


@router.get("/settings")
async def api_settings_get() -> dict[str, Any]:

    return await asyncio.to_thread(settings_store.get)


@router.put("/settings")
async def api_settings_put(body: SettingsBody) -> dict[str, Any]:

    try:
        return await asyncio.to_thread(settings_store.update, body.data)
    except ValueError as exc:
        raise http_error(str(exc), 400)
    except Exception as exc:
        raise fail(exc)


@router.get("/settings/export")
async def api_settings_export() -> Response:

    data = await asyncio.to_thread(settings_store.export_data)

    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="molly-settings.json"',
        },
    )


@router.post("/settings/import")
async def api_settings_import(body: SettingsBody) -> dict[str, Any]:

    try:
        return await asyncio.to_thread(settings_store.import_data, body.data)
    except ValueError as exc:
        raise http_error(str(exc), 400)
    except Exception as exc:
        raise fail(exc)


@router.get("/settings/backups")
async def api_settings_backups() -> dict[str, Any]:

    return {"backups": await asyncio.to_thread(settings_store.list_backups)}


@router.post("/settings/backups")
async def api_settings_backup_now() -> dict[str, Any]:

    name = await asyncio.to_thread(settings_store.backup_now)

    return {"ok": True, "name": name}


@router.post("/settings/restore")
async def api_settings_restore(body: BackupBody) -> dict[str, Any]:

    try:
        return await asyncio.to_thread(settings_store.restore_backup, body.name)
    except ValueError as exc:
        raise http_error(str(exc), 400)
    except Exception as exc:
        raise fail(exc)


@router.get("/settings/default-prompt")
async def api_default_prompt() -> dict[str, Any]:

    s = await asyncio.to_thread(settings_store.get)

    return {"prompt": rag.default_base_prompt(s["assistant_name"])}


# ============================================================
# Ollama
# ============================================================

@router.get("/ollama/status")
async def api_ollama_status(url: str | None = None) -> dict[str, Any]:
    """
    Проверка подключения и список моделей. Параметр url позволяет
    проверить адрес до сохранения настроек.
    """

    s = await asyncio.to_thread(settings_store.get)

    base = (url or s["ollama_url"]).strip().rstrip("/")

    if not base.startswith(("http://", "https://")):
        raise http_error("Адрес должен начинаться с http:// или https://", 400)

    result = await asyncio.to_thread(llm.check, base, s["model"])

    result["current_model"] = s["model"]

    return result


# ============================================================
# Чаты
# ============================================================

class ChatTitle(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)


@router.get("/chats")
async def api_chats(request: Request) -> dict[str, Any]:

    owner = owner_of(request)

    return {"chats": await asyncio.to_thread(chats_store.list_chats, owner)}


@router.post("/chats")
async def api_chats_create(request: Request) -> dict[str, Any]:

    owner = owner_of(request)

    return await asyncio.to_thread(chats_store.create_chat, owner)


@router.get("/chats/{chat_id}")
async def api_chat_get(chat_id: int, request: Request) -> dict[str, Any]:

    owner = owner_of(request)

    chat = await asyncio.to_thread(chats_store.get_chat, owner, chat_id)

    if chat is None:
        raise http_error("Чат не найден.", 404)

    return chat


@router.patch("/chats/{chat_id}")
async def api_chat_rename(chat_id: int, body: ChatTitle, request: Request) -> dict[str, Any]:

    owner = owner_of(request)

    try:
        ok = await asyncio.to_thread(chats_store.rename_chat, owner, chat_id, body.title)
    except ValueError as exc:
        raise http_error(str(exc), 400)

    if not ok:
        raise http_error("Чат не найден.", 404)

    return {"ok": True}


@router.delete("/chats/{chat_id}")
async def api_chat_delete(chat_id: int, request: Request) -> dict[str, Any]:

    owner = owner_of(request)

    ok = await asyncio.to_thread(chats_store.delete_chat, owner, chat_id)

    if not ok:
        raise http_error("Чат не найден.", 404)

    return {"ok": True}


# ============================================================
# Ответ модели (потоковый, NDJSON)
# ============================================================
#
# Каждая строка — JSON-событие:
#   {"type":"meta","chat_id":..,"title":..}
#   {"type":"sources","sources":[...]}
#   {"type":"token","text":"..."}
#   {"type":"error","code":"...","message":"..."}
#   {"type":"done","message_id":..,"stats":{...},"stopped":bool}
# Ошибки Ollama приходят событием error (HTTP 200), а не 500.

class ChatRequest(BaseModel):
    chat_id: int | None = None
    message: str = Field(..., min_length=1, max_length=40000)
    model: str | None = None
    use_documents: bool | None = None
    # Повтор после ошибки: не дублировать уже сохранённое сообщение пользователя.
    retry: bool = False


def _event(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


@router.post("/chat/stream")
async def api_chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:

    owner = owner_of(request)

    message = body.message.strip()

    if not message:
        raise http_error("Пустое сообщение.", 400)

    settings = await asyncio.to_thread(settings_store.get)

    if body.chat_id is None:
        chat = await asyncio.to_thread(
            chats_store.create_chat,
            owner,
            message.replace("\n", " ")[:60],
        )
        chat_id = chat["id"]
        title = chat["title"]
    else:
        existing = await asyncio.to_thread(chats_store.get_chat, owner, body.chat_id)
        if existing is None:
            raise http_error("Чат не найден.", 404)
        chat_id = body.chat_id
        title = existing["title"]

    skip_user = False

    if body.retry:
        last = await asyncio.to_thread(chats_store.recent_messages, chat_id, 1)
        skip_user = bool(last and last[0]["role"] == "user" and last[0]["content"] == message)

    if not skip_user:
        await asyncio.to_thread(chats_store.add_message, chat_id, "user", message)

    model = (body.model or settings["model"] or "").strip()

    use_docs = settings["use_documents"] if body.use_documents is None else body.use_documents

    async def generate():

        yield _event({"type": "meta", "chat_id": chat_id, "title": title})

        sources: list[dict[str, Any]] = []
        context = ""

        project = get_project_manager().project_path

        docs_requested = bool(use_docs and project is not None)

        if docs_requested:

            try:
                sources, context = await asyncio.to_thread(
                    rag.retrieve,
                    project,
                    message,
                    settings["top_k"],
                    max(2000, int(settings["num_ctx"])),
                )
            except Exception:
                logger.exception("Не удалось подобрать фрагменты документов")

            if sources:
                yield _event({"type": "sources", "sources": sources})

        if not model:
            yield _event({
                "type": "error",
                "code": "no_model",
                "message": "Модель не выбрана. Выберите её в верхней панели или в Настройки → Модель.",
            })
            return

        system = rag.build_system_prompt(settings, context, docs_requested)

        history = await asyncio.to_thread(chats_store.recent_messages, chat_id, 12)

        messages = [{"role": "system", "content": system}] + history

        options = {
            "temperature": settings["temperature"],
            "num_ctx": settings["num_ctx"],
            "num_predict": settings["max_tokens"],
        }

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def post(item: tuple[str, Any]) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # loop уже закрыт (остановка сервера)

        def worker() -> None:
            try:
                for item in llm.stream_chat(
                    settings["ollama_url"],
                    model,
                    messages,
                    options,
                    settings["keep_alive"],
                    stop,
                ):
                    post(("chunk", item))
                post(("end", None))
            except llm.OllamaError as exc:
                post(("error", {"code": exc.code, "message": exc.message}))
            except Exception as exc:
                logger.exception("Сбой потока ответа модели")
                post(("error", {"code": "internal", "message": f"Внутренняя ошибка: {exc}"}))

        threading.Thread(target=worker, name="molly-chat", daemon=True).start()

        parts: list[str] = []
        saved_id: int | None = None
        completed = False
        started = time.monotonic()
        stats: dict[str, Any] = {}

        def save(stopped: bool) -> int | None:
            text = "".join(parts)
            if not text:
                return None
            return chats_store.add_message(
                chat_id, "assistant", text, sources, stopped=stopped
            )

        try:

            while True:

                kind, data = await queue.get()

                if kind == "chunk":

                    piece = (data.get("message") or {}).get("content") or ""

                    if piece:
                        parts.append(piece)
                        yield _event({"type": "token", "text": piece})

                    if data.get("done"):
                        completed = True
                        count = data.get("eval_count") or 0
                        stats = {
                            "tokens": count,
                            "seconds": round(time.monotonic() - started, 1),
                        }

                elif kind == "error":

                    saved_id = save(stopped=True)

                    yield _event({"type": "error", **data})

                    return

                else:
                    break

            saved_id = save(stopped=not completed)

            yield _event({
                "type": "done",
                "message_id": saved_id,
                "stats": stats,
                "stopped": not completed,
            })

        finally:

            # Пользователь нажал «Стоп» / закрыл вкладку: прерываем
            # запрос к Ollama и сохраняем уже полученный текст.
            stop.set()

            if saved_id is None and parts and not completed:
                try:
                    save(stopped=True)
                except Exception:
                    logger.exception("Не удалось сохранить прерванный ответ")

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
