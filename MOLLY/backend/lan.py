from __future__ import annotations

import hmac
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

import settings_store
from logging_setup import get_logger

logger = get_logger("lan")

# ============================================================
# Доступ из локальной сети
# ============================================================
#
# Модель безопасности:
#
#  * Основной сервер слушает ТОЛЬКО 127.0.0.1 — с этого компьютера
#    всё доступно без входа (владелец).
#  * «Сервер в локальной сети» — отдельный listener на 0.0.0.0:<порт>.
#    Пока он выключен, порт закрыт. Включается/выключается кнопкой.
#  * Подключиться к нему можно только из частных сетей
#    (192.168.x.x, 10.x.x.x, 172.16-31.x.x, link-local). Адреса из
#    интернета отклоняются, даже если порт проброшен на роутере.
#  * Вход — по токену доступа (+ имя пользователя). 5 неверных
#    попыток с одного IP за 5 минут блокируют IP на 5 минут.
#  * Пользователям LAN доступны чат, поиск и просмотр документов.
#    Настройки, почта, выбор папки, индексация, управление сервером —
#    только на основном компьютере.
#  * Защита от атак через браузер: проверка заголовков Host (DNS
#    rebinding) и Origin (CSRF) для запросов, меняющих данные.

SESSION_COOKIE = "molly_sid"
SESSION_TTL = 7 * 24 * 3600
ONLINE_WINDOW = 90

MAX_FAILS = 5
FAIL_WINDOW = 300


class LanError(Exception):
    pass


# ------------------------------------------------------------
# IP helpers
# ------------------------------------------------------------

def _ip(value: str):
    try:
        ip = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def is_loopback(host: str | None) -> bool:
    ip = _ip(host or "")
    return bool(ip and ip.is_loopback)


def is_private(host: str | None) -> bool:
    ip = _ip(host or "")
    return bool(ip and (ip.is_private or ip.is_link_local or ip.is_loopback))


def lan_ips() -> list[str]:
    """Адреса этого компьютера в локальных сетях (основной — первым)."""

    found: list[str] = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    result: list[str] = []

    for value in found:
        ip = _ip(value)
        if ip and ip.is_private and not ip.is_loopback and value not in result:
            result.append(value)

    return result


def port_in_use(host: str, port: int) -> bool:

    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex((probe, port)) == 0:
            return True

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return True

    return False


# ------------------------------------------------------------
# Сессии
# ------------------------------------------------------------

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}
_fails: dict[str, list[float]] = {}


def _clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f:<>\"']", "", name or "").strip()
    return name[:40]


def login(token: str, name: str, ip: str, user_agent: str) -> tuple[str, str]:
    """Возвращает (id сессии, имя). LanError — с понятным сообщением."""

    name = _clean_name(name)

    if not name:
        raise LanError("Введите ваше имя.")

    now = time.time()

    with _lock:

        attempts = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = attempts

        if len(attempts) >= MAX_FAILS:
            raise LanError(
                "Слишком много неверных попыток. Подождите 5 минут."
            )

        expected = settings_store.get()["lan"].get("token") or ""

        given = (token or "").strip().upper()

        if not expected or not hmac.compare_digest(
            given.encode("utf-8"), expected.upper().encode("utf-8")
        ):
            _fails[ip].append(now)
            logger.warning("LAN: неверный токен от %s", ip)
            raise LanError("Неверный токен доступа.")

        _fails.pop(ip, None)

        sid = secrets.token_urlsafe(24)

        _sessions[sid] = {
            "name": name,
            "ip": ip,
            "agent": (user_agent or "")[:120],
            "created": now,
            "last_seen": now,
        }

    logger.info("LAN: вход пользователя %r с %s", name, ip)

    return sid, name


def get_session(sid: str | None) -> dict[str, Any] | None:

    if not sid:
        return None

    now = time.time()

    with _lock:

        session = _sessions.get(sid)

        if session is None:
            return None

        if now - session["last_seen"] > SESSION_TTL:
            del _sessions[sid]
            return None

        session["last_seen"] = now

        return dict(session)


def logout(sid: str | None) -> None:
    if sid:
        with _lock:
            _sessions.pop(sid, None)


def revoke_all() -> None:
    with _lock:
        _sessions.clear()


def kick(prefix: str) -> bool:
    with _lock:
        for sid in list(_sessions):
            if sid.startswith(prefix):
                del _sessions[sid]
                return True
    return False


def list_clients() -> list[dict[str, Any]]:

    now = time.time()

    with _lock:
        items = [
            {
                "id": sid[:8],
                "name": s["name"],
                "ip": s["ip"],
                "agent": s["agent"],
                "since": int(s["created"]),
                "last_seen": int(s["last_seen"]),
                "online": now - s["last_seen"] < ONLINE_WINDOW,
            }
            for sid, s in _sessions.items()
        ]

    return sorted(items, key=lambda c: -c["last_seen"])


def owner_for(name: str) -> str:
    return "lan:" + name.casefold()


# ------------------------------------------------------------
# Какие пути доступны пользователям LAN
# ------------------------------------------------------------

PUBLIC_PREFIXES = ("/static/",)
PUBLIC_EXACT = {"/", "/health", "/favicon.ico", "/api/auth/login", "/api/auth/status"}

LAN_ALLOWED_PREFIXES = (
    "/api/chats",
    "/api/chat/",
    "/api/search",
    "/api/file/",
    "/api/ui-config",
    "/api/auth/",
)

LAN_ALLOWED_GET = {"/api/project", "/api/index/status"}


def lan_allowed(method: str, path: str) -> bool:

    if path in LAN_ALLOWED_GET and method in ("GET", "HEAD"):
        return True

    return any(path.startswith(p) for p in LAN_ALLOWED_PREFIXES)


def is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


# ------------------------------------------------------------
# ASGI middleware
# ------------------------------------------------------------

def _host_allowed(host_header: str) -> bool:

    host = host_header.strip().lower()

    if host.startswith("["):
        hostname = host[1:].split("]")[0]
    else:
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host

    if hostname == "localhost" or _ip(hostname) is not None:
        return True

    machine = socket.gethostname().lower()

    return hostname == machine or hostname == machine + ".local"


class AccessMiddleware:
    """Чистый ASGI (без BaseHTTPMiddleware — не ломает потоковые ответы)."""

    SECURITY_HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
    ]

    def __init__(self, app: Any, lan_server: "LanServer") -> None:
        self.app = app
        self.lan = lan_server

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:

        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        if scope["type"] != "http":
            # WebSocket в приложении не используется.
            await send({"type": "websocket.close", "code": 1008})
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method = scope["method"]
        path = scope["path"]

        async def reject(status: int, message: str, **extra: Any) -> None:
            body = json.dumps({"error": message, **extra}, ensure_ascii=False).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    *self.SECURITY_HEADERS,
                ],
            })
            await send({"type": "http.response.body", "body": body})

        # 1. Host — защита от DNS rebinding.
        if not _host_allowed(headers.get("host", "")):
            await reject(400, "Недопустимый заголовок Host.")
            return

        # 2. Origin — защита от CSRF (запросы с чужих сайтов).
        if method not in ("GET", "HEAD", "OPTIONS"):
            origin = headers.get("origin")
            if origin and origin != "null":
                if urlparse(origin).netloc.lower() != headers.get("host", "").lower():
                    await reject(403, "Запрос с чужого сайта отклонён.")
                    return
            elif origin == "null":
                await reject(403, "Запрос отклонён.")
                return

        # 3. Кто пришёл.
        client_host = (scope.get("client") or ("", 0))[0]
        server_port = (scope.get("server") or ("", 0))[1]

        lan_port = self.lan.port if self.lan.running else None

        is_local = is_loopback(client_host) and server_port != lan_port

        if is_local:
            scope["molly"] = {"is_local": True, "owner": "local", "name": ""}

        else:

            if not self.lan.running:
                await reject(
                    403,
                    "Доступ из сети выключен. Включите «Сервер в локальной "
                    "сети» в настройках МОЛЛИ на основном компьютере.",
                )
                return

            if not is_private(client_host):
                logger.warning("LAN: отклонён внешний адрес %s", client_host)
                await reject(403, "Доступ разрешён только из локальной сети.")
                return

            sid = None
            for part in headers.get("cookie", "").split(";"):
                key, _, value = part.strip().partition("=")
                if key == SESSION_COOKIE:
                    sid = value

            session = get_session(sid)

            scope["molly"] = {
                "is_local": False,
                "owner": owner_for(session["name"]) if session else None,
                "name": session["name"] if session else "",
                "sid": sid if session else None,
                "ip": client_host,
            }

            if not is_public(path):

                if session is None:
                    await reject(401, "Требуется вход.", login_required=True)
                    return

                if not lan_allowed(method, path):
                    await reject(
                        403,
                        "Эта функция доступна только на компьютере, "
                        "где запущена МОЛЛИ.",
                    )
                    return

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                extra = list(self.SECURITY_HEADERS)
                if path.startswith("/api/"):
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ------------------------------------------------------------
# Второй listener (0.0.0.0)
# ------------------------------------------------------------

class LanServer:

    def __init__(self) -> None:
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._server is not None
            and bool(getattr(self._server, "started", False))
        )

    def start(self, app: Any, port: int) -> None:

        if self.running:
            return

        if port_in_use("0.0.0.0", port):
            raise LanError(
                f"Порт {port} занят другой программой. Выберите другой порт."
            )

        import uvicorn

        # log_config=None — иначе uvicorn перенастроит логирование
        # основного сервера.
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_config=None,
            access_log=False,
            lifespan="off",
        )

        self._server = uvicorn.Server(config)
        self.error = None
        self.port = port

        self._thread = threading.Thread(
            target=self._run,
            name="molly-lan",
            daemon=True,
        )
        self._thread.start()

        deadline = time.time() + 8

        while time.time() < deadline:
            if getattr(self._server, "started", False):
                logger.info("LAN-сервер запущен на порту %d", port)
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.05)

        message = self.error or "Не удалось запустить сервер (нет ответа за 8 секунд)."

        self.stop()

        raise LanError(message)

    def _run(self) -> None:
        try:
            self._server.run()
        except BaseException as exc:
            self.error = str(exc) or exc.__class__.__name__
            logger.exception("LAN-сервер аварийно остановлен")

    def stop(self) -> None:

        server, thread = self._server, self._thread

        if server is not None:
            server.should_exit = True

        if thread is not None and thread.is_alive():
            thread.join(timeout=8)

        self._server = None
        self._thread = None
        self.port = None

        revoke_all()

        logger.info("LAN-сервер остановлен")


lan_server = LanServer()
