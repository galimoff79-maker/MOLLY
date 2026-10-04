from __future__ import annotations

import base64
import binascii
import imaplib
import re
import smtplib
import socket
import ssl
import threading
import time
import uuid
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from html.parser import HTMLParser
from typing import Any, Iterator

import secrets_store
import settings_store
from logging_setup import get_logger

logger = get_logger("mail")

# ============================================================
# Яндекс.Почта (и любой IMAP/SMTP-сервер)
# ============================================================
#
# Способ подключения: IMAP (чтение) + SMTP (отправка) по SSL.
# Это штатный способ Яндекс.Почты. Для входа нужен «пароль
# приложения» (не пароль от аккаунта), а в настройках Яндекс.Почты
# должен быть включён доступ по IMAP. OAuth не используется: для него
# требуется регистрация собственного приложения у Яндекса.
#
# Пароль хранится только в защищённом хранилище (secrets_store).

PASSWORD_SECRET = "mail_password"
TIMEOUT = 25
PAGE_SIZE = 25
MAX_BODY_CHARS = 200_000
MAX_ATTACHMENT_TOTAL = 25 * 1024 * 1024
DRAFT_TTL = 3600

YANDEX_HINT = (
    "Для Яндекс.Почты: в настройках почты (Почтовые программы) включите "
    "доступ по протоколу IMAP, создайте «пароль приложения» в "
    "id.yandex.ru → Безопасность → Пароли приложений и введите его "
    "здесь вместо пароля от аккаунта."
)


class MailError(Exception):

    def __init__(self, message: str, code: str = "mail_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ------------------------------------------------------------
# Modified UTF-7 (имена IMAP-папок)
# ------------------------------------------------------------

def utf7_decode(value: str) -> str:

    def repl(match: re.Match) -> str:
        chunk = match.group(1)
        if chunk == "":
            return "&"
        b64 = chunk.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(b64).decode("utf-16-be")
        except (binascii.Error, UnicodeDecodeError):
            return match.group(0)

    return re.sub(r"&([^-]*)-", repl, value)


def utf7_encode(value: str) -> str:

    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            b64 = base64.b64encode(raw).decode("ascii").rstrip("=")
            out.append("&" + b64.replace("/", ",") + "-")
            buf.clear()

    for ch in value:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)

    flush()

    return "".join(out)


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ------------------------------------------------------------
# Подключение
# ------------------------------------------------------------

def _credentials() -> dict[str, Any]:

    mail = settings_store.get()["mail"]

    email_addr = (mail.get("email") or "").strip()

    if not email_addr:
        raise MailError(
            "Почта не настроена: укажите адрес и пароль приложения "
            "в разделе «Почта» → «Подключение».",
            "not_configured",
        )

    try:
        password = secrets_store.get_secret(PASSWORD_SECRET)
    except secrets_store.SecretsUnavailable as exc:
        raise MailError(str(exc), "secrets_unavailable")

    if not password:
        raise MailError(
            "Пароль почты не задан. Введите пароль приложения в "
            "разделе «Почта» → «Подключение».",
            "not_configured",
        )

    return {**mail, "email": email_addr, "password": password}


def _network_error() -> MailError:
    return MailError(
        "Нет соединения с почтовым сервером. Проверьте подключение к "
        "интернету и адреса серверов в настройках почты.",
        "network",
    )


def _auth_error(detail: str = "") -> MailError:
    return MailError(
        "Сервер отклонил вход. " + YANDEX_HINT,
        "auth_failed",
    )


@contextmanager
def imap_session() -> Iterator[imaplib.IMAP4_SSL]:

    creds = _credentials()

    conn = None

    try:

        conn = imaplib.IMAP4_SSL(
            creds["imap_host"],
            creds["imap_port"],
            timeout=TIMEOUT,
            ssl_context=ssl.create_default_context(),
        )

        try:
            conn.login(creds["email"], creds["password"])
        except imaplib.IMAP4.error as exc:
            raise _auth_error(str(exc))

        yield conn

    except MailError:
        raise

    except (socket.timeout, TimeoutError, ssl.SSLError, OSError, imaplib.IMAP4.abort) as exc:
        logger.warning("IMAP: сетевая ошибка: %s", exc)
        raise _network_error()

    except imaplib.IMAP4.error as exc:
        raise MailError(f"Ошибка почтового сервера: {exc}", "imap_error")

    finally:

        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def _select(conn: imaplib.IMAP4_SSL, folder_raw: str, readonly: bool = True) -> int:

    status, data = conn.select(_quote(folder_raw), readonly=readonly)

    if status != "OK":
        raise MailError("Не удалось открыть папку.", "folder_error")

    try:
        return int(data[0])
    except (TypeError, ValueError, IndexError):
        return 0


def _check_uid(uid: str) -> str:

    uid = str(uid)

    if not uid.isdigit():
        raise MailError("Неверный идентификатор письма.", "bad_uid")

    return uid


# ------------------------------------------------------------
# Проверка подключения / папки
# ------------------------------------------------------------

def test_connection() -> dict[str, Any]:

    result: dict[str, Any] = {"imap": None, "smtp": None}

    try:
        with imap_session() as conn:
            status, data = conn.list()
            result["imap"] = {"ok": status == "OK", "folders": len(data or [])}
    except MailError as exc:
        result["imap"] = {"ok": False, "error": exc.message, "code": exc.code}

    try:
        creds = _credentials()
        smtp = _smtp_connect(creds)
        try:
            smtp.noop()
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        result["smtp"] = {"ok": True}
    except MailError as exc:
        result["smtp"] = {"ok": False, "error": exc.message, "code": exc.code}

    result["ok"] = bool(
        result["imap"] and result["imap"].get("ok")
        and result["smtp"] and result["smtp"].get("ok")
    )

    return result


_LIST_RE = re.compile(
    rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$'
)


def parse_list_line(line: bytes) -> dict[str, Any] | None:

    match = _LIST_RE.match(line.strip())

    if not match:
        return None

    name = match.group("name").decode("utf-8", "replace").strip()

    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")

    flags = match.group("flags").decode("ascii", "replace")

    if "\\Noselect" in flags:
        return None

    return {
        "raw": name,
        "name": utf7_decode(name),
        "flags": flags,
    }


FOLDER_TITLES = {
    "INBOX": "Входящие",
    "Sent": "Отправленные",
    "Drafts": "Черновики",
    "Trash": "Удалённые",
    "Spam": "Спам",
    "Outbox": "Исходящие",
}

FOLDER_ORDER = ["INBOX", "Sent", "Drafts", "Trash", "Spam", "Outbox"]


def list_folders() -> list[dict[str, Any]]:

    with imap_session() as conn:
        status, data = conn.list()

    if status != "OK":
        raise MailError("Не удалось получить список папок.", "folder_error")

    folders = []

    for line in data or []:
        if isinstance(line, bytes):
            item = parse_list_line(line)
            if item:
                item["title"] = FOLDER_TITLES.get(item["name"], item["name"])
                folders.append(item)

    def order(f: dict[str, Any]) -> tuple[int, str]:
        try:
            return FOLDER_ORDER.index(f["name"]), ""
        except ValueError:
            return len(FOLDER_ORDER), f["name"].lower()

    return sorted(folders, key=order)


# ------------------------------------------------------------
# Список писем
# ------------------------------------------------------------

def _parse_header_fetch(data: list[Any]) -> list[dict[str, Any]]:

    parser = BytesHeaderParser(policy=policy.default)

    items: list[dict[str, Any]] = []

    i = 0

    while i < len(data):

        entry = data[i]

        if isinstance(entry, tuple):

            meta = entry[0].decode("ascii", "replace")

            # Часть серверов присылает UID/FLAGS уже после тела.
            if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                meta += " " + data[i + 1].decode("ascii", "replace")

            uid = re.search(r"UID (\d+)", meta)

            if uid:

                flags = re.search(r"FLAGS \(([^)]*)\)", meta)
                headers = parser.parsebytes(entry[1])

                items.append(
                    {
                        "uid": uid.group(1),
                        "from": str(headers.get("From", "") or ""),
                        "to": str(headers.get("To", "") or ""),
                        "subject": str(headers.get("Subject", "") or "(без темы)"),
                        "date": str(headers.get("Date", "") or ""),
                        "unread": "\\Seen" not in (flags.group(1) if flags else ""),
                    }
                )

        i += 1

    return items


def _fetch_headers(conn: imaplib.IMAP4_SSL, uids: list[str]) -> list[dict[str, Any]]:

    if not uids:
        return []

    status, data = conn.uid(
        "FETCH",
        ",".join(uids),
        "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])",
    )

    if status != "OK":
        raise MailError("Не удалось получить заголовки писем.", "fetch_error")

    by_uid = {m["uid"]: m for m in _parse_header_fetch(data)}

    # Порядок как в uids (новые первыми).
    return [by_uid[u] for u in uids if u in by_uid]


def list_messages(
    folder_raw: str,
    page: int = 1,
    query: str | None = None,
) -> dict[str, Any]:

    page = max(1, page)

    with imap_session() as conn:

        _select(conn, folder_raw)

        uids: list[str] | None = None

        query = (query or "").strip()

        if query:

            # Серверный поиск по теме/отправителю/тексту (UTF-8).
            try:
                q = b'"' + query.replace("\\", "\\\\").replace('"', '\\"').encode("utf-8") + b'"'
                status, data = conn.uid(
                    "SEARCH", "CHARSET", "UTF-8",
                    "OR", "OR", "SUBJECT", q, "FROM", q, "TEXT", q,
                )
                if status == "OK":
                    uids = (data[0] or b"").decode().split()
            except (imaplib.IMAP4.error, UnicodeError):
                uids = None

            # Запасной вариант: фильтр по последним письмам.
            if uids is None:
                status, data = conn.uid("SEARCH", None, "ALL")
                all_uids = (data[0] or b"").decode().split() if status == "OK" else []
                recent = list(reversed(all_uids))[:300]
                needle = query.lower()
                uids = [
                    m["uid"]
                    for m in _fetch_headers(conn, recent)
                    if needle in (m["subject"] + " " + m["from"]).lower()
                ]
                uids = list(reversed(uids))

        else:

            status, data = conn.uid("SEARCH", None, "ALL")

            if status != "OK":
                raise MailError("Не удалось получить список писем.", "search_error")

            uids = (data[0] or b"").decode().split()

        total = len(uids)

        newest_first = list(reversed(uids))

        start = (page - 1) * PAGE_SIZE

        chunk = newest_first[start:start + PAGE_SIZE]

        messages = _fetch_headers(conn, chunk)

    return {
        "messages": messages,
        "total": total,
        "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
    }


# ------------------------------------------------------------
# Письмо целиком
# ------------------------------------------------------------

class _HtmlText(HTMLParser):

    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:

    parser = _HtmlText()

    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)

    text = "".join(parser.parts)

    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _attachments(msg: Any) -> list[Any]:

    result = []

    for part in msg.walk():

        if part.is_multipart():
            continue

        filename = part.get_filename()

        if part.get_content_disposition() == "attachment" or (
            filename and part.get_content_disposition() != "inline"
        ):
            result.append(part)

    return result


def _fetch_raw(conn: imaplib.IMAP4_SSL, uid: str) -> bytes:

    status, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")

    if status != "OK":
        raise MailError("Не удалось загрузить письмо.", "fetch_error")

    for entry in data:
        if isinstance(entry, tuple):
            return entry[1]

    raise MailError("Письмо не найдено.", "not_found")


def parse_message(raw: bytes, uid: str) -> dict[str, Any]:

    msg = BytesParser(policy=policy.default).parsebytes(raw)

    body = ""

    plain = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))

    try:
        if plain is not None:
            body = plain.get_content()
        elif html_part is not None:
            body = html_to_text(html_part.get_content())
    except (LookupError, UnicodeError):
        body = "[Не удалось декодировать текст письма]"

    attachments = []

    for index, part in enumerate(_attachments(msg)):
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            {
                "index": index,
                "name": part.get_filename() or f"вложение-{index + 1}",
                "type": part.get_content_type(),
                "size": len(payload),
            }
        )

    return {
        "uid": uid,
        "from": str(msg.get("From", "") or ""),
        "to": str(msg.get("To", "") or ""),
        "cc": str(msg.get("Cc", "") or ""),
        "subject": str(msg.get("Subject", "") or "(без темы)"),
        "date": str(msg.get("Date", "") or ""),
        "message_id": str(msg.get("Message-ID", "") or ""),
        "references": str(msg.get("References", "") or ""),
        "reply_to": str(msg.get("Reply-To", "") or ""),
        "body": (body or "")[:MAX_BODY_CHARS],
        "attachments": attachments,
    }


def get_message(folder_raw: str, uid: str) -> dict[str, Any]:

    uid = _check_uid(uid)

    with imap_session() as conn:
        _select(conn, folder_raw)
        raw = _fetch_raw(conn, uid)

    return parse_message(raw, uid)


def get_attachment(folder_raw: str, uid: str, index: int) -> tuple[str, str, bytes]:

    uid = _check_uid(uid)

    with imap_session() as conn:
        _select(conn, folder_raw)
        raw = _fetch_raw(conn, uid)

    msg = BytesParser(policy=policy.default).parsebytes(raw)

    parts = _attachments(msg)

    if not 0 <= index < len(parts):
        raise MailError("Вложение не найдено.", "not_found")

    part = parts[index]

    return (
        part.get_filename() or f"attachment-{index + 1}",
        part.get_content_type(),
        part.get_payload(decode=True) or b"",
    )


def move_message(folder_raw: str, uid: str, target_raw: str) -> None:

    uid = _check_uid(uid)

    with imap_session() as conn:

        _select(conn, folder_raw, readonly=False)

        status, _ = conn.uid("COPY", uid, _quote(target_raw))

        if status != "OK":
            raise MailError("Не удалось переместить письмо.", "move_error")

        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()


# ------------------------------------------------------------
# Отправка
# ------------------------------------------------------------

_ADDR_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def parse_addresses(value: str | list[str]) -> list[str]:

    if isinstance(value, list):
        value = ",".join(value)

    value = (value or "").replace(";", ",")

    result = []

    for _, addr in getaddresses([value]):
        addr = addr.strip()
        if addr:
            if not _ADDR_RE.match(addr):
                raise MailError(f"Некорректный адрес: {addr}", "bad_address")
            result.append(addr)

    return result


def _smtp_connect(creds: dict[str, Any]) -> smtplib.SMTP:

    context = ssl.create_default_context()

    try:

        if int(creds["smtp_port"]) == 587:
            smtp = smtplib.SMTP(creds["smtp_host"], 587, timeout=TIMEOUT)
            smtp.starttls(context=context)
        else:
            smtp = smtplib.SMTP_SSL(
                creds["smtp_host"],
                int(creds["smtp_port"]),
                timeout=TIMEOUT,
                context=context,
            )

        smtp.login(creds["email"], creds["password"])

        return smtp

    except smtplib.SMTPAuthenticationError:
        raise _auth_error()

    except (socket.timeout, TimeoutError, ssl.SSLError, OSError):
        raise _network_error()

    except smtplib.SMTPException as exc:
        raise MailError(f"Ошибка SMTP: {exc}", "smtp_error")


def build_message(draft: dict[str, Any], from_addr: str) -> EmailMessage:

    msg = EmailMessage()

    msg["From"] = from_addr
    msg["To"] = ", ".join(draft["to"])

    if draft.get("cc"):
        msg["Cc"] = ", ".join(draft["cc"])

    msg["Subject"] = draft["subject"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])

    if draft.get("in_reply_to"):
        msg["In-Reply-To"] = draft["in_reply_to"]
        refs = (draft.get("references") or "").strip()
        msg["References"] = (refs + " " + draft["in_reply_to"]).strip()

    msg.set_content(draft["body"])

    for att in draft.get("attachments", []):
        maintype, _, subtype = (att.get("type") or "application/octet-stream").partition("/")
        msg.add_attachment(
            att["data"],
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att["name"],
        )

    return msg


# Черновики живут в памяти до подтверждения отправки.
_drafts: dict[str, dict[str, Any]] = {}
_drafts_lock = threading.Lock()


def create_draft(
    to: Any,
    cc: Any,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]] | None = None,
    reply_folder: str | None = None,
    reply_uid: str | None = None,
) -> dict[str, Any]:

    recipients = parse_addresses(to)

    if not recipients:
        raise MailError("Укажите получателя.", "bad_address")

    cc_list = parse_addresses(cc or "")

    subject = (subject or "").strip() or "(без темы)"

    if len(subject) > 500:
        raise MailError("Тема слишком длинная.", "bad_subject")

    prepared = []
    total = 0

    for att in attachments or []:

        try:
            data = base64.b64decode(att.get("content_b64", ""), validate=True)
        except (binascii.Error, ValueError):
            raise MailError("Вложение повреждено.", "bad_attachment")

        total += len(data)

        if total > MAX_ATTACHMENT_TOTAL:
            raise MailError(
                "Вложения слишком большие (максимум 25 МБ суммарно).",
                "attachments_too_large",
            )

        prepared.append(
            {
                "name": re.sub(r'[\\/:*?"<>|\r\n]', "_", att.get("name") or "file"),
                "type": att.get("type") or "application/octet-stream",
                "data": data,
            }
        )

    draft: dict[str, Any] = {
        "to": recipients,
        "cc": cc_list,
        "subject": subject,
        "body": body or "",
        "attachments": prepared,
        "created": time.time(),
    }

    if reply_folder and reply_uid:
        original = get_message(reply_folder, reply_uid)
        draft["in_reply_to"] = original["message_id"]
        draft["references"] = original["references"]

    draft_id = uuid.uuid4().hex

    with _drafts_lock:

        now = time.time()

        for key in [k for k, v in _drafts.items() if now - v["created"] > DRAFT_TTL]:
            del _drafts[key]

        if len(_drafts) >= 30:
            oldest = min(_drafts, key=lambda k: _drafts[k]["created"])
            del _drafts[oldest]

        _drafts[draft_id] = draft

    return {
        "id": draft_id,
        "to": recipients,
        "cc": cc_list,
        "subject": subject,
        "body": draft["body"],
        "attachments": [
            {"name": a["name"], "size": len(a["data"])} for a in prepared
        ],
    }


def discard_draft(draft_id: str) -> None:
    with _drafts_lock:
        _drafts.pop(draft_id, None)


def send_draft(draft_id: str) -> dict[str, Any]:

    with _drafts_lock:
        draft = _drafts.get(draft_id)

    if draft is None:
        raise MailError(
            "Черновик не найден или устарел. Создайте письмо заново.",
            "draft_not_found",
        )

    creds = _credentials()

    msg = build_message(draft, creds["email"])

    smtp = _smtp_connect(creds)

    try:

        refused = smtp.send_message(
            msg,
            to_addrs=draft["to"] + draft["cc"],
        )

    except smtplib.SMTPRecipientsRefused:
        raise MailError("Сервер отклонил адрес получателя.", "recipients_refused")

    except smtplib.SMTPException as exc:
        raise MailError(f"Не удалось отправить письмо: {exc}", "smtp_error")

    except (socket.timeout, TimeoutError, OSError):
        raise _network_error()

    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    discard_draft(draft_id)

    logger.info(
        "Письмо отправлено: получателей=%d, тема=%r",
        len(draft["to"]) + len(draft["cc"]),
        draft["subject"][:60],
    )

    return {"sent": True, "refused": list((refused or {}).keys())}
