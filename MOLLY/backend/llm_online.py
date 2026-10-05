from __future__ import annotations

"""Клиент FreeLLMAPI (OpenAI-compatible) и реестр онлайн-моделей.

Архитектура:

    llm_online.FreeLLMAPIClient   — HTTP-слой (/v1/models, /api/ping, chat SSE)
    ModelRegistry                 — список моделей + кэш на диске
    rank_model()                  — рейтинг «мощности» модели
    ModelHealthManager            — cooldown после 429/503/timeout
    ModelSelector                 — AUTO / ручной выбор + порядок fallback
    stream_chat_with_fallback()   — запрос с автоматическим переподключением

Индекс документов при этом не затрагивается: RAG-контекст формирует MOLLY,
модель получает только найденные фрагменты.
"""

import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

from config import (
    DATA_DIR,
    FREELLMAPI_API_KEY_ENV,
    FREELLMAPI_BASE_URL,
    FREELLMAPI_MAX_REQUEST_BYTES,
)
from logging_setup import get_logger

logger = get_logger("model_manager")

# Прокси для локального FreeLLMAPI отключены намеренно (как у Ollama).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CONNECT_TIMEOUT = 5.0
STREAM_TIMEOUT = 600.0
MODELS_REFRESH_SECONDS = 120.0
MAX_ATTEMPTS = 4          # максимум моделей на один запрос пользователя
COOLDOWN_DEFAULT = 60.0   # секунд; для 429 используется Retry-After
COOLDOWN_MAX = 600.0

REGISTRY_CACHE_FILE = DATA_DIR / "models_cache.json"

_lock = threading.RLock()


class OnlineError(Exception):
    """Ошибка онлайн-провайдера с понятным пользователю сообщением."""

    def __init__(
        self,
        message: str,
        code: str = "online_error",
        *,
        retryable: bool = False,
        model: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.model = model
        self.retry_after = retry_after


# ============================================================
# Клиент
# ============================================================

def mask_key(key: str | None) -> str:
    """Безопасное представление ключа для UI/логов (никогда не сам ключ)."""
    if not key:
        return ""
    return "•" * 8 + (key[-4:] if len(key) > 4 else "")


def resolve_api_key(settings: dict[str, Any] | None = None) -> str:
    """API-ключ: защищённое хранилище → переменная окружения → настройки.

    В исходном коде ключ не хранится. В логи не попадает.
    """
    try:
        import secrets_store

        stored = secrets_store.get_secret("freellmapi_key")
        if stored:
            return stored
    except Exception:
        # Хранилище недоступно (не Windows) — идем дальше по списку.
        pass

    env_key = os.getenv(FREELLMAPI_API_KEY_ENV, "").strip()
    if env_key:
        return env_key

    if settings:
        # Для тестов/окружений без DPAPI допустимо задать ключ в настройках.
        inline = str(settings.get("freellmapi", {}).get("api_key", "") or "").strip()
        if inline:
            return inline

    return ""


class FreeLLMAPIClient:
    """Тонкий HTTP-клиент OpenAI-compatible API FreeLLMAPI."""

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key

    # ---------- низкоуровневый слой ----------

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = CONNECT_TIMEOUT,
    ):
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        return _OPENER.open(request, timeout=timeout)

    @staticmethod
    def _classify_http(exc: urllib.error.HTTPError, model: str | None) -> OnlineError:
        detail = ""
        try:
            body = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
                detail = str(
                    (parsed.get("error") or {}).get("message")
                    if isinstance(parsed.get("error"), dict)
                    else parsed.get("error")
                    or body
                )[:300]
            except ValueError:
                detail = body[:300]
        except Exception:
            pass

        low = detail.lower()
        code = exc.code
        retryable = code in (408, 429, 500, 502, 503, 504) or "unavailable" in low

        retry_after = None
        if code == 429:
            try:
                retry_after = float(exc.headers.get("Retry-After", "") or "")
            except ValueError:
                retry_after = None

        if code == 401:
            message = "FreeLLMAPI отклонила API-ключ. Проверьте его в Настройки → FreeLLMAPI."
            kind = "bad_key"
        elif code == 404 or "not found" in low:
            message = f"Модель «{model}» недоступна во FreeLLMAPI."
            kind = "model_not_found"
        elif code == 429:
            message = "FreeLLMAPI временно перегружена (лимит запросов)."
            kind = "rate_limited"
        elif code in (502, 503) or "provider" in low or "candidate model" in low:
            message = "Провайдер модели временно недоступен."
            kind = "provider_unavailable"
        else:
            message = f"FreeLLMAPI вернула ошибку {code}: {detail or exc.reason}"
            kind = "http_error"

        return OnlineError(message, kind, retryable=retryable, model=model, retry_after=retry_after)

    def _classify_conn(self, exc: Exception) -> OnlineError:
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return OnlineError(
                "FreeLLMAPI не ответила вовремя.",
                "timeout",
                retryable=True,
            )
        return OnlineError(
            f"FreeLLMAPI недоступна по адресу {self.base_url}. "
            "Запустите FreeLLMAPI или выберите локальную модель Ollama.",
            "unavailable",
            retryable=True,
        )

    # ---------- публичные методы ----------

    def ping(self) -> bool:
        """/api/ping, с фолбэком на /v1/models."""
        for path in ("/api/ping", "/v1/models"):
            try:
                with self._request("GET", path) as resp:
                    resp.read()
                return True
            except Exception:
                continue
        return False

    def list_models(self) -> list[dict[str, Any]]:
        try:
            with self._request("GET", "/v1/models") as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise self._classify_http(exc, None)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise self._classify_conn(exc)

        result: list[dict[str, Any]] = []
        for item in data.get("data", []) or []:
            mid = str(item.get("id") or item.get("name") or "").strip()
            if not mid:
                continue
            result.append(
                {
                    "id": mid,
                    "name": str(item.get("name") or mid),
                    "context_length": item.get("context_length") or item.get("max_model_len"),
                    "created": item.get("created"),
                    "raw": item,
                }
            )
        return result

    def stream_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any],
        stop: threading.Event,
        max_request_bytes: int = FREELLMAPI_MAX_REQUEST_BYTES,
    ) -> Iterator[str]:
        """Потоковый чат (SSE). Выдаёт куски текста ответа.

        Тело запроса автоматически урезается под лимит API (128 КБ):
        сначала сокращается контекст RAG (system-сообщение).
        """
        temperature = options.get("temperature", 0.3)
        max_tokens = options.get("num_predict") or options.get("max_tokens") or 1024

        msgs = [dict(m) for m in messages]

        def _body() -> dict[str, Any]:
            return {
                "model": model,
                "messages": msgs,
                "stream": True,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

        def _size() -> int:
            return len(json.dumps(_body(), ensure_ascii=False).encode("utf-8"))

        # Автоматическое сокращение контекста под лимит запроса.
        while _size() > max_request_bytes:
            sys_idx = next((i for i, m in enumerate(msgs) if m.get("role") == "system"), None)
            content = msgs[sys_idx]["content"] if sys_idx is not None else ""
            if sys_idx is None or len(content) < 800:
                break
            cut = int(len(content) * 0.7)
            marker = "\n…(контекст сокращён под лимит API)…"
            msgs[sys_idx]["content"] = content[: max(200, cut - len(marker))] + marker

        payload = _body()
        size = _size()
        if size > max_request_bytes:
            raise OnlineError(
                "Запрос слишком большой даже после сокращения контекста. "
                "Уменьшите число фрагментов (Настройки → top_k).",
                "too_large",
            )

        logger.info("[ModelManager] Запрос отправлен: %s (%d байт)", model, size)

        try:
            response = self._request("POST", "/v1/chat/completions", payload, STREAM_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise self._classify_http(exc, model)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise self._classify_conn(exc)

        try:
            while not stop.is_set():
                try:
                    line = response.readline()
                except (socket.timeout, TimeoutError):
                    raise OnlineError(
                        "FreeLLMAPI перестала отвечать (таймаут ожидания).",
                        "timeout",
                        retryable=True,
                        model=model,
                    )

                if not line:
                    break

                line = line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue

                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break

                try:
                    item = json.loads(chunk)
                except ValueError:
                    continue

                err = item.get("error")
                if err:
                    text = err.get("message") if isinstance(err, dict) else str(err)
                    raise OnlineError(
                        f"FreeLLMAPI: {text}",
                        "api_error",
                        retryable=bool(re.search(r"unavailable|overload|cooldown|provider|key", str(text), re.I)),
                        model=model,
                    )

                choices = item.get("choices") or []
                if choices:
                    piece = ((choices[0].get("delta") or {}).get("content")) or ""
                    if piece:
                        yield piece
        finally:
            try:
                response.close()
            except Exception:
                pass


# ============================================================
# Рейтинг моделей
# ============================================================

# Токенизированный разбор имени: «r1», «o3», «480b» ищутся по границам слов,
# чтобы не путать «gpt-4o-mini» или «deepseek-v4» с r1/o3.
_REASONING_RE = re.compile(
    r"reason|thinking|deepseek-r1|\bqwq\b|\br1\b|[\-/]r1[\-.]|(?:^|[\-_/ ])o[13](?:[\-.\d$]|[\-_/]|$)",
    re.I,
)
_CODING_RE = re.compile(r"coder|codellama|starcoder|devstral|codegeex|\bcode\b", re.I)
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([bkm])\b", re.I)
_NAME_NOISE_RE = re.compile(r"[^a-z0-9]+")

_KNOWS_CONTEXT = ("gpt-4", "gpt-4o", "claude", "gemini", "deepseek-chat", "kimi")

# Известные семейства reasoning-моделей (в именах нет слов «reason»/«r1»).
_KNOWN_REASONING = (
    "deepseek-r1", "qwq", "o1-", "o3-", "o4-mini", "gpt-5",
    "thinking", "-think", "reasoning",
)


def _looks_reasoning(hay: str) -> bool:
    low = hay.lower()
    if _REASONING_RE.search(hay):
        return True
    return any(k in low for k in _KNOWN_REASONING)


def _extract_size_b(model_id: str) -> float:
    """Оценка размера модели в миллиардах параметров по имени."""
    best = 0.0
    for num, unit in _SIZE_RE.findall(_NAME_NOISE_RE.sub("-", model_id)):
        value = float(num)
        u = unit.lower()
        if u == "b":
            params = value
        elif u == "k":
            params = value / 1000.0
        else:  # m — миллионы
            params = value / 1000.0
        best = max(best, params)
    return best


def rank_model(info: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Рейтинг «мощности» модели. Чем выше — тем лучше для МОЛЛИ.

    Учитывает: reasoning, coding, context window, размер/класс, свежесть.
    """
    model_id = str(info.get("id") or "")
    name = str(info.get("name") or model_id)
    hay = f"{model_id} {name}"

    score = 0.0
    flags: dict[str, Any] = {}

    ctx = info.get("context_length")
    if not isinstance(ctx, (int, float)) and any(k in hay.lower() for k in _KNOWS_CONTEXT):
        ctx = 128_000  # известные семейства с большим контекстом
    ctx = int(ctx or 0)
    flags["context_window"] = ctx
    if ctx >= 200_000:
        score += 40.0
    elif ctx >= 128_000:
        score += 30.0
    elif ctx >= 32_000:
        score += 15.0
    elif ctx >= 8_000:
        score += 5.0

    reasoning = _looks_reasoning(hay)
    coding = bool(_CODING_RE.search(hay))
    flags["reasoning"] = reasoning
    flags["coding"] = coding
    if reasoning:
        score += 25.0
    if coding:
        score += 15.0

    size_b = _extract_size_b(hay)
    flags["size_b"] = size_b
    if size_b:
        score += min(30.0, size_b * 0.6)

    created = info.get("created")
    if isinstance(created, (int, float)) and created > 0:
        age_days = max(0.0, (time.time() - created) / 86400.0)
        score += max(0.0, 10.0 - age_days / 90.0)  # актуальность

    premium = re.search(r"(?:^|[\s\-/])(opus|sonnet|pro|ultra|max|preview)(?:$|[\s\-/])", hay, re.I)
    if premium:
        score += 10.0

    return score, flags


# ============================================================
# Реестр моделей + кэш
# ============================================================

@dataclass
class RegistryEntry:
    info: dict[str, Any]
    score: float
    flags: dict[str, Any]


class ModelRegistry:
    """Актуальный список моделей FreeLLMAPI с дисковым кэшем."""

    def __init__(self, client_factory) -> None:
        self._client_factory = client_factory
        self._entries: list[RegistryEntry] = []
        self._loaded_at = 0.0
        self._source = "empty"
        self._last_error: str | None = None

    def _save_cache(self, models: list[dict[str, Any]]) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = REGISTRY_CACHE_FILE.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"saved_at": time.time(), "models": models}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, REGISTRY_CACHE_FILE)
        except OSError:
            logger.warning("Не удалось сохранить кэш моделей", exc_info=True)

    def _load_cache(self) -> list[dict[str, Any]]:
        try:
            if REGISTRY_CACHE_FILE.exists():
                data = json.loads(REGISTRY_CACHE_FILE.read_text(encoding="utf-8"))
                models = data.get("models")
                if isinstance(models, list):
                    return [m for m in models if isinstance(m, dict) and m.get("id")]
        except Exception:
            logger.warning("Кэш моделей повреждён — игнорируется", exc_info=True)
        return []

    def _apply(self, models: list[dict[str, Any]], source: str) -> None:
        entries: list[RegistryEntry] = []
        for info in models:
            score, flags = rank_model(info)
            entries.append(RegistryEntry(info=info, score=score, flags=flags))
        entries.sort(key=lambda e: (-e.score, e.info.get("id", "")))
        with _lock:
            self._entries = entries
            self._loaded_at = time.time()
            self._source = source

    def refresh(self, force: bool = False) -> bool:
        """Обновить список из API. При недоступности API — оставить кэш/текущий."""
        with _lock:
            fresh = (time.time() - self._loaded_at) < MODELS_REFRESH_SECONDS and self._entries
            if fresh and not force:
                return True

        try:
            models = self._client_factory().list_models()
        except OnlineError as exc:
            self._last_error = exc.message
            if not self._entries:
                cached = self._load_cache()
                if cached:
                    self._apply(cached, "cache")
                    logger.info("[ModelManager] API недоступен — использован кэш: %d моделей", len(cached))
            return False
        except Exception as exc:  # сеть/парсинг — не роняем приложение
            self._last_error = str(exc)
            logger.exception("Неожиданная ошибка обновления списка моделей")
            return False

        self._last_error = None
        self._apply(models, "api")
        self._save_cache(models)
        logger.info("[ModelManager] Получено моделей: %d", len(models))
        return True

    def entries(self) -> list[RegistryEntry]:
        with _lock:
            return list(self._entries)

    def count(self) -> int:
        with _lock:
            return len(self._entries)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def source(self) -> str:
        with _lock:
            return self._source


# ============================================================
# Здоровье моделей (cooldown)
# ============================================================

class ModelHealthManager:
    """Временный cooldown упавших моделей; автоматическое восстановление."""

    def __init__(self) -> None:
        self._cooldown_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    def mark_failure(self, model: str, retry_after: float | None = None) -> float:
        with _lock:
            fails = self._failures.get(model, 0) + 1
            self._failures[model] = fails
            duration = retry_after or min(COOLDOWN_MAX, COOLDOWN_DEFAULT * (2 ** min(fails - 1, 3)))
            until = time.time() + duration
            self._cooldown_until[model] = until
        logger.info("[ModelManager] Модель %s помечена недоступной на %.0f c", model, duration)
        return duration

    def mark_success(self, model: str) -> None:
        with _lock:
            self._cooldown_until.pop(model, None)
            self._failures.pop(model, None)

    def available(self, model: str) -> bool:
        with _lock:
            until = self._cooldown_until.get(model)
            if until is None:
                return True
            if time.time() >= until:
                # Cooldown истёк — модель снова participates в выборе.
                del self._cooldown_until[model]
                self._failures.pop(model, None)
                logger.info("[ModelManager] Cooldown модели %s истёк — она снова доступна", model)
                return True
            return False

    def retry_in(self, model: str) -> float:
        with _lock:
            until = self._cooldown_until.get(model)
            return max(0.0, until - time.time()) if until else 0.0


# ============================================================
# Выбор модели (AUTO / ручная) + fallback
# ============================================================

def _task_hints(query: str) -> set[str]:
    """Определение типа задачи по вопросу (для предпочтений рейтинга)."""
    q = (query or "").lower()
    hints: set[str] = []
    if re.search(r"код|програм|скрипт|python|функци|баг", q):
        hints.add("coding")
    if re.search(r"сравн|противореч|анализ|проверь|почему|рассчит|объём|цена|ведомост", q):
        hints.add("reasoning")
    if re.search(r"\bкс-\d|аоср|аоок|ожр|сертификат|паспорт|протокол|акт|чертеж|раздел", q):
        hints.add("docs")
    return hints


class ModelSelector:
    """AUTO: лучшая доступная; manual: выбранная + fallback на AUTO."""

    def __init__(self, registry: ModelRegistry, health: ModelHealthManager) -> None:
        self.registry = registry
        self.health = health

    def candidates(self, query: str = "", preferred: str | None = None) -> list[str]:
        """Упорядоченный список моделей для попыток запроса."""
        hints = _task_hints(query)
        entries = [e for e in self.registry.entries() if self.health.available(e.info["id"])]

        def sort_key(e: RegistryEntry) -> tuple:
            bonus = 0.0
            if "coding" in hints and e.flags.get("coding"):
                bonus += 12.0
            if "reasoning" in hints and e.flags.get("reasoning"):
                bonus += 10.0
            if "docs" in hints:
                bonus += min(8.0, e.flags.get("context_window", 0) / 32_000)
            return (-(e.score + bonus), e.info.get("id", ""))

        ranked = [e.info["id"] for e in sorted(entries, key=sort_key)]

        if preferred and preferred != "auto":
            if self.health.available(preferred):
                # Ручной выбор: начинаем с него, остальные — как fallback.
                rest = [m for m in ranked if m != preferred]
                return [preferred] + rest
            logger.info("[ModelManager] Выбранная модель %s недоступна — fallback AUTO", preferred)
        return ranked

    def best_available(self) -> str | None:
        ids = self.candidates()
        return ids[0] if ids else None


# ============================================================
# Единый менеджер + запрос с fallback
# ============================================================

_managers: dict[str, "ModelManager"] = {}
_manager_lock = threading.Lock()


class ModelManager:
    def __init__(self) -> None:
        self._default_base = FREELLMAPI_BASE_URL
        self._registry: ModelRegistry | None = None
        self._health = ModelHealthManager()
        self._selector: ModelSelector | None = None

    # ---- конфигурация ----

    def _client_for(self, settings: dict[str, Any]) -> FreeLLMAPIClient:
        freellm = settings.get("freellmapi", {})
        base = (freellm.get("url") or self._default_base).strip().rstrip("/")
        return FreeLLMAPIClient(base, resolve_api_key(settings))

    def registry(self, settings: dict[str, Any]) -> ModelRegistry:
        global_registry = self._registry
        if global_registry is None:
            with _manager_lock:
                if self._registry is None:
                    holder: dict[str, Any] = {"settings": settings}
                    self._registry = ModelRegistry(lambda: self._client_for(holder["settings"]))
                    self._holder = holder
                    self._selector = ModelSelector(self._registry, self._health)
        if hasattr(self, "_holder"):
            self._holder["settings"] = settings
        return self._registry

    def selector(self, settings: dict[str, Any]) -> ModelSelector:
        self.registry(settings)
        assert self._selector is not None
        return self._selector

    # ---- статус для UI ----

    def status(self, settings: dict[str, Any]) -> dict[str, Any]:
        reg = self.registry(settings)
        sel = self.selector(settings)
        reg.refresh()
        entries = reg.entries()
        mode = settings.get("model_mode", "auto")
        manual = settings.get("online_model", "")
        active = None
        if mode == "manual" and manual and sel.health.available(manual):
            active = manual
        else:
            active = sel.best_available()

        key = resolve_api_key(settings)
        return {
            "ok": bool(entries),
            "url": settings.get("freellmapi", {}).get("url") or self._default_base,
            "connected": reg.source == "api" and bool(entries),
            "models_count": len(entries),
            "active_model": active,
            "active_name": next(
                (e.info.get("name") for e in entries if e.info.get("id") == active), active
            ),
            "mode": mode,
            "key_configured": bool(key),
            "key_masked": mask_key(key),
            "error": reg.last_error if not entries else None,
            "models": [
                {
                    "id": e.info["id"],
                    "name": e.info.get("name") or e.info["id"],
                    "score": round(e.score, 1),
                    "context_window": e.flags.get("context_window", 0),
                    "reasoning": bool(e.flags.get("reasoning")),
                    "coding": bool(e.flags.get("coding")),
                    "available": self._health.available(e.info["id"]),
                    "cooldown_in": round(self._health.retry_in(e.info["id"]), 1),
                }
                for e in entries
            ],
        }

    # ---- основной путь: запрос с автоматическим fallback ----

    def stream_chat(
        self,
        settings: dict[str, Any],
        messages: list[dict[str, str]],
        options: dict[str, Any],
        stop: threading.Event,
        query: str = "",
    ) -> Iterator[tuple[str, Any]]:
        """События: ("model", id) при старте/смене, ("token", text), ("end", None).

        Бросает OnlineError, когда все разумные попытки исчерпаны.
        """
        sel = self.selector(settings)
        reg = sel.registry
        reg.refresh()

        mode = settings.get("model_mode", "auto")
        preferred = settings.get("online_model") if mode == "manual" else None
        candidates = sel.candidates(query=query, preferred=preferred)[:MAX_ATTEMPTS]

        if not candidates:
            if reg.count() == 0:
                raise OnlineError(
                    "FreeLLMAPI подключен, но сейчас нет доступных моделей. "
                    "МОЛЛИ автоматически повторит проверку.",
                    "no_models",
                )
            raise OnlineError(
                "Все модели FreeLLMAPI временно недоступны "
                "(перегрузка провайдеров). МОЛЛИ повторит автоматически — попробуйте через минуту.",
                "all_cooling",
                retryable=True,
            )

        client = self._client_for(settings)
        last_error: OnlineError | None = None

        for model in candidates:
            if stop.is_set():
                return
            yielded_any = False
            try:
                yield ("model", model)
                for piece in client.stream_chat(model, messages, options, stop):
                    yielded_any = True
                    yield ("token", piece)
                sel.health.mark_success(model)
                logger.info("[ModelManager] Запрос успешно выполнен: %s", model)
                yield ("end", model)
                return
            except OnlineError as exc:
                exc.model = exc.model or model
                if stop.is_set():
                    return
                last_error = exc
                if yielded_any:
                    # Обрыв в середине ответа: не повторяем запрос целиком,
                    # иначе пользователь увидит дублирование текста.
                    break
                if exc.retryable:
                    sel.health.mark_failure(model, exc.retry_after)
                    logger.info(
                        "[ModelManager] Модель %s исключена (%s), fallback: следующая",
                        model, exc.code,
                    )
                else:
                    # Плохой ключ / модель не найдена — короткая изоляция,
                    # чтобы AUTO не крутился вокруг неё без паузы.
                    sel.health.mark_failure(model, 30.0)
                continue

        if last_error is not None:
            raise OnlineError(
                last_error.message + " Все доступные модели перебраны, ответ не получен.",
                last_error.code,
                retryable=True,
            )


def get_manager(settings: dict[str, Any] | None = None) -> ModelManager:
    """Менеджер, привязанный к адресу FreeLLMAPI (для тестов и смены URL)."""
    base = FREELLMAPI_BASE_URL
    if settings:
        base = (settings.get("freellmapi", {}).get("url") or base).strip().rstrip("/")
    with _manager_lock:
        manager = _managers.get(base)
        if manager is None:
            manager = ModelManager()
            manager._default_base = base
            _managers[base] = manager
        return manager
