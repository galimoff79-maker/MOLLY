from __future__ import annotations

import base64
import json
import os
import sys
import threading
from typing import Callable

from config import DATA_DIR
from logging_setup import get_logger

logger = get_logger("secrets")

# ============================================================
# Безопасное хранение паролей
# ============================================================
#
# Windows: DPAPI (CryptProtectData, область «текущий пользователь»).
# Файл data/secrets.json содержит только зашифрованные блоки;
# расшифровать их может только тот же пользователь Windows на этом
# же компьютере. Пароли не попадают ни в исходный код, ни в
# настройки, ни в экспорт, ни в логи.
#
# Не Windows: безопасного хранилища без сторонних пакетов нет —
# сохранение отклоняется (SecretsUnavailable).

SECRETS_FILE = DATA_DIR / "secrets.json"

_lock = threading.Lock()


class SecretsUnavailable(Exception):
    pass


def _dpapi_functions() -> tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]:

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    def _blob(data: bytes):
        buffer = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
        ), buffer

    def protect(data: bytes) -> bytes:
        blob_in, keep = _blob(data)
        blob_out = DATA_BLOB()
        if not crypt32.CryptProtectData(
            ctypes.byref(blob_in), "MOLLY", None, None, None, 0,
            ctypes.byref(blob_out),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)

    def unprotect(data: bytes) -> bytes:
        blob_in, keep = _blob(data)
        blob_out = DATA_BLOB()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)

    return protect, unprotect


_backend: tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]] | None = None


def _get_backend():

    global _backend

    if _backend is None:

        if sys.platform != "win32":
            raise SecretsUnavailable(
                "Безопасное хранилище паролей доступно только в Windows."
            )

        _backend = _dpapi_functions()

    return _backend


def is_available() -> bool:
    try:
        _get_backend()
        return True
    except Exception:
        return False


def _read() -> dict[str, str]:

    if not SECRETS_FILE.exists():
        return {}

    try:
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Не удалось прочитать хранилище секретов")
        return {}


def _write(data: dict[str, str]) -> None:

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    tmp = SECRETS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, SECRETS_FILE)


def set_secret(name: str, value: str) -> None:

    protect, _ = _get_backend()

    blob = protect(value.encode("utf-8"))

    with _lock:
        data = _read()
        data[name] = base64.b64encode(blob).decode("ascii")
        _write(data)


def get_secret(name: str) -> str | None:

    with _lock:
        encoded = _read().get(name)

    if not encoded:
        return None

    _, unprotect = _get_backend()

    try:
        return unprotect(base64.b64decode(encoded)).decode("utf-8")
    except Exception:
        # Другой пользователь Windows / другой компьютер / повреждение.
        logger.warning("Не удалось расшифровать секрет %s", name)
        return None


def has_secret(name: str) -> bool:
    with _lock:
        return bool(_read().get(name))


def delete_secret(name: str) -> None:
    with _lock:
        data = _read()
        if name in data:
            del data[name]
            _write(data)
