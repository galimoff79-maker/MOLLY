from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from typing import Any, Iterator
from urllib.parse import urlparse

from logging_setup import get_logger

logger = get_logger("llm")

# ============================================================
# Клиент Ollama (только стандартная библиотека)
# ============================================================
#
# Ollama работает локально, поэтому системные прокси
# (HTTP_PROXY и т.п.) намеренно отключены: на Windows они
# часто ломают обращение к 127.0.0.1.

_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)

CONNECT_TIMEOUT = 5
# Первый токен холодной модели может идти минуты (загрузка в RAM).
STREAM_TIMEOUT = 600


class OllamaError(Exception):
    """Ошибка с понятным пользователю сообщением."""

    def __init__(self, message: str, code: str = "ollama_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _unavailable(base_url: str) -> OllamaError:
    return OllamaError(
        "Ollama недоступна по адресу "
        f"{base_url}. Запустите Ollama (значок в системном трее или "
        "команда «ollama serve») и проверьте адрес в "
        "Настройки → Модель.",
        "ollama_unavailable",
    )


def _error_from_http(exc: urllib.error.HTTPError, base_url: str, model: str | None) -> OllamaError:

    detail = ""

    try:
        body = exc.read().decode("utf-8", "replace")
        detail = json.loads(body).get("error", body)
    except Exception:
        pass

    low = detail.lower()

    if exc.code == 404 or "not found" in low:
        name = model or "выбранная модель"
        return OllamaError(
            f"Модель «{name}» не найдена в Ollama. Выберите другую модель "
            f"в списке или загрузите её командой: ollama pull {name}",
            "model_not_found",
        )

    if "memory" in low:
        return OllamaError(
            "Модели не хватает оперативной памяти. Выберите модель "
            "поменьше или уменьшите «Контекст» в Настройки → Модель. "
            f"({detail})",
            "out_of_memory",
        )

    return OllamaError(
        f"Ollama вернула ошибку {exc.code}: {detail or exc.reason}",
        "ollama_http_error",
    )


def _open(base_url: str, path: str, payload: dict[str, Any] | None, timeout: float):

    url = base_url.rstrip("/") + path

    data = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )

    return _OPENER.open(request, timeout=timeout)


def check(base_url: str, current_model: str | None = None) -> dict[str, Any]:
    """
    Проверка подключения: версия и список моделей.
    Никогда не бросает исключений — возвращает {ok, error, hint, ...}.
    """

    result: dict[str, Any] = {
        "ok": False,
        "url": base_url,
        "version": None,
        "models": [],
        "error": None,
        "model_present": False,
    }

    try:

        with _open(base_url, "/api/version", None, CONNECT_TIMEOUT) as resp:
            result["version"] = json.loads(resp.read().decode("utf-8")).get("version")

        with _open(base_url, "/api/tags", None, CONNECT_TIMEOUT) as resp:
            tags = json.loads(resp.read().decode("utf-8"))

        result["models"] = sorted(
            (
                {
                    "name": m.get("name") or m.get("model"),
                    "size": m.get("size", 0),
                    "modified": m.get("modified_at"),
                }
                for m in tags.get("models", [])
                if m.get("name") or m.get("model")
            ),
            key=lambda m: m["name"],
        )

        result["ok"] = True

        if current_model:
            names = {m["name"] for m in result["models"]}
            result["model_present"] = (
                current_model in names
                or f"{current_model}:latest" in names
            )

    except urllib.error.HTTPError as exc:
        result["error"] = _error_from_http(exc, base_url, None).message

    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
        result["error"] = _unavailable(base_url).message

    except Exception as exc:
        logger.exception("Неожиданная ошибка проверки Ollama")
        result["error"] = f"Не удалось проверить Ollama: {exc}"

    return result


def stream_chat(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    options: dict[str, Any],
    keep_alive: str,
    stop: threading.Event,
) -> Iterator[dict[str, Any]]:
    """
    Потоковый чат. Выдаёт словари Ollama
    ({"message": {"content": ...}, "done": bool, ...}).
    Прерывается, когда stop выставлен. OllamaError — с понятным текстом.
    """

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": options,
    }

    # keep_alive: "5m", "0" и т.п. Число без единицы — секунды.
    if keep_alive:
        payload["keep_alive"] = (
            int(keep_alive) if keep_alive.lstrip("-").isdigit() else keep_alive
        )

    try:
        response = _open(base_url, "/api/chat", payload, STREAM_TIMEOUT)

    except urllib.error.HTTPError as exc:
        raise _error_from_http(exc, base_url, model)

    except (urllib.error.URLError, ConnectionError):
        raise _unavailable(base_url)

    except (socket.timeout, TimeoutError):
        raise OllamaError(
            "Ollama не ответила вовремя. Возможно, модель ещё загружается — "
            "попробуйте ещё раз через минуту.",
            "timeout",
        )

    try:

        while not stop.is_set():

            try:
                line = response.readline()
            except (socket.timeout, TimeoutError):
                raise OllamaError(
                    "Ollama перестала отвечать (таймаут ожидания).",
                    "timeout",
                )

            if not line:
                break

            line = line.strip()

            if not line:
                continue

            try:
                item = json.loads(line.decode("utf-8"))
            except ValueError:
                continue

            if item.get("error"):
                raise _error_from_http_text(str(item["error"]), model)

            yield item

            if item.get("done"):
                break

    finally:
        try:
            response.close()
        except Exception:
            pass


def _error_from_http_text(detail: str, model: str) -> OllamaError:

    low = detail.lower()

    if "not found" in low:
        return OllamaError(
            f"Модель «{model}» не найдена в Ollama. Загрузите её командой: "
            f"ollama pull {model}",
            "model_not_found",
        )

    if "memory" in low:
        return OllamaError(
            "Модели не хватает оперативной памяти. Выберите модель "
            f"поменьше или уменьшите «Контекст». ({detail})",
            "out_of_memory",
        )

    return OllamaError(f"Ошибка Ollama: {detail}", "ollama_error")


def is_local_url(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")
