from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

import mail_service
import secrets_store
import settings_store
from api import http_error
from logging_setup import get_logger
from mail_service import MailError

logger = get_logger("api_mail")

router = APIRouter(prefix="/api/mail", tags=["Почта"])

# Все эти маршруты доступны только на основном компьютере
# (пользователям LAN они закрыты в lan.lan_allowed).


async def _run(func, *args, **kwargs):

    try:
        return await asyncio.to_thread(func, *args, **kwargs)

    except MailError as exc:
        status = 502 if exc.code == "network" else 400
        raise http_error(exc.message, status, code=exc.code)


# ------------------------------------------------------------
# Подключение
# ------------------------------------------------------------

class CredentialsBody(BaseModel):
    email: str = Field(..., max_length=200)
    password: str | None = Field(None, max_length=200)
    imap_host: str | None = None
    imap_port: int | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    confirm_send: bool | None = None


def _status() -> dict[str, Any]:

    mail = settings_store.get()["mail"]

    return {
        "email": mail["email"],
        "imap_host": mail["imap_host"],
        "imap_port": mail["imap_port"],
        "smtp_host": mail["smtp_host"],
        "smtp_port": mail["smtp_port"],
        "confirm_send": mail["confirm_send"],
        "has_password": secrets_store.has_secret(mail_service.PASSWORD_SECRET),
        "secure_storage": secrets_store.is_available(),
        "hint": mail_service.YANDEX_HINT,
    }


@router.get("/status")
async def api_mail_status() -> dict[str, Any]:

    return await asyncio.to_thread(_status)


def _save_credentials(body: CredentialsBody) -> dict[str, Any]:

    patch = {
        k: v
        for k, v in body.model_dump().items()
        if k in ("email", "imap_host", "imap_port", "smtp_host", "smtp_port", "confirm_send")
        and v is not None
    }

    try:
        settings_store.update({"mail": patch})
    except ValueError as exc:
        raise http_error(str(exc), 400)

    if body.password:
        try:
            secrets_store.set_secret(mail_service.PASSWORD_SECRET, body.password)
        except secrets_store.SecretsUnavailable as exc:
            raise http_error(str(exc), 400, code="secrets_unavailable")

    return _status()


@router.put("/credentials")
async def api_mail_credentials(body: CredentialsBody) -> dict[str, Any]:

    return await asyncio.to_thread(_save_credentials, body)


@router.delete("/credentials")
async def api_mail_credentials_delete() -> dict[str, Any]:

    def work() -> dict[str, Any]:
        secrets_store.delete_secret(mail_service.PASSWORD_SECRET)
        return _status()

    return await asyncio.to_thread(work)


@router.post("/test")
async def api_mail_test() -> dict[str, Any]:

    return await _run(mail_service.test_connection)


# ------------------------------------------------------------
# Папки и письма
# ------------------------------------------------------------

@router.get("/folders")
async def api_mail_folders() -> dict[str, Any]:

    return {"folders": await _run(mail_service.list_folders)}


@router.get("/messages")
async def api_mail_messages(
    folder: str = Query("INBOX"),
    page: int = Query(1, ge=1, le=10000),
    q: str | None = Query(None, max_length=200),
) -> dict[str, Any]:

    return await _run(mail_service.list_messages, folder, page, q)


@router.get("/message")
async def api_mail_message(
    folder: str = Query(...),
    uid: str = Query(...),
) -> dict[str, Any]:

    return await _run(mail_service.get_message, folder, uid)


@router.get("/attachment")
async def api_mail_attachment(
    folder: str = Query(...),
    uid: str = Query(...),
    index: int = Query(..., ge=0),
) -> Response:

    name, content_type, data = await _run(mail_service.get_attachment, folder, uid, index)

    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        },
    )


class MoveBody(BaseModel):
    folder: str
    uid: str
    target: str


@router.post("/move")
async def api_mail_move(body: MoveBody) -> dict[str, Any]:

    await _run(mail_service.move_message, body.folder, body.uid, body.target)

    return {"ok": True}


# ------------------------------------------------------------
# Создание и отправка (с подтверждением)
# ------------------------------------------------------------

class AttachmentBody(BaseModel):
    name: str = Field(..., max_length=255)
    type: str | None = None
    content_b64: str


class DraftBody(BaseModel):
    to: str
    cc: str | None = ""
    subject: str = ""
    body: str = ""
    attachments: list[AttachmentBody] = []
    reply_folder: str | None = None
    reply_uid: str | None = None


class SendBody(BaseModel):
    confirmed: bool = False


@router.post("/drafts")
async def api_mail_draft(body: DraftBody) -> dict[str, Any]:
    """Шаг 1: письмо только готовится (ничего не отправляется)."""

    return await _run(
        mail_service.create_draft,
        body.to,
        body.cc,
        body.subject,
        body.body,
        [a.model_dump() for a in body.attachments],
        body.reply_folder,
        body.reply_uid,
    )


@router.post("/drafts/{draft_id}/send")
async def api_mail_send(draft_id: str, body: SendBody) -> dict[str, Any]:
    """Шаг 2: отправка. Сервер требует явное подтверждение пользователя."""

    settings = await asyncio.to_thread(settings_store.get)

    if settings["mail"]["confirm_send"] and not body.confirmed:
        raise http_error(
            "Отправка не подтверждена пользователем.",
            400,
            code="confirmation_required",
        )

    return await _run(mail_service.send_draft, draft_id)


@router.delete("/drafts/{draft_id}")
async def api_mail_draft_delete(draft_id: str) -> dict[str, Any]:

    mail_service.discard_draft(draft_id)

    return {"ok": True}
