"""Тесты Model Manager для FreeLLMAPI.

Проверяются: получение /v1/models, AUTO-выбор лучшей модели, fallback при
429/503/timeout, cooldown и автоматическое возвращение к мощной модели,
ручной выбор, сокращение контекста под лимит API, маскирование ключа,
работоспособность при недоступном FreeLLMAPI и общий RAG-контекст для всех
моделей (индекс не зависит от выбранной модели).
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm_online  # noqa: E402


# ============================================================
# Mock FreeLLMAPI server
# ============================================================

MODELS = [
    {"id": "gemma-4b", "created": 1700000000},
    {"id": "qwen3-coder-480b-a22b", "created": 1750000000},
    {"id": "deepseek-v4-pro", "created": 1740000000},
]


class MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fail_map: dict[str, tuple[int, int]] = {}  # model -> (http_code, times)
        self.chat_calls: list[tuple[str, dict]] = []
        self.models_available = True
        self.retry_after: str | None = None

    def set_fail(self, model: str, code: int, times: int = 1) -> None:
        with self.lock:
            self.fail_map[model] = (code, times)

    def take_fail(self, model: str) -> int | None:
        with self.lock:
            entry = self.fail_map.get(model)
            if not entry:
                return None
            code, times = entry
            times -= 1
            if times <= 0:
                del self.fail_map[model]
            else:
                self.fail_map[model] = (code, times)
            return code


class Handler(BaseHTTPRequestHandler):
    state: MockState = MockState()
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # тишина сервера
        pass

    def _json(self, code: int, payload: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/ping":
            self._json(200, {"status": "ok"})
        elif self.path == "/v1/models":
            if not self.state.models_available:
                self._json(503, {"error": {"message": "no usable provider key"}})
                return
            data = [{"id": m["id"], "created": m["created"]} for m in MODELS]
            self._json(200, {"object": "list", "data": data})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json(400, {"error": "bad json"})
            return
        model = payload.get("model", "")
        self.state.chat_calls.append((model, payload))
        fail = self.state.take_fail(model)
        if fail is not None:
            headers = {}
            if self.state.retry_after:
                headers["Retry-After"] = self.state.retry_after
            self._json(fail, {"error": {"message": f"mock {fail}"}}, headers)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(data: bytes) -> None:
            self.wfile.write(b"%X\r\n" % len(data))
            self.wfile.write(data + b"\r\n")

        for piece in ["Привет", " от ", model]:
            chunk = {"choices": [{"delta": {"content": piece}}]}
            send(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        send(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture(scope="module")
def server():
    state = MockState()
    Handler.state = state
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield url, state
    srv.shutdown()


@pytest.fixture()
def manager(server, tmp_path, monkeypatch):
    url, state = server
    state.chat_calls.clear()
    state.fail_map.clear()
    state.models_available = True
    state.retry_after = None
    monkeypatch.setattr(llm_online, "REGISTRY_CACHE_FILE", tmp_path / "models_cache.json")
    mgr = llm_online.ModelManager()
    mgr._default_base = url
    settings = {
        "freellmapi": {"enabled": True, "url": url},
        "model_mode": "auto",
        "online_model": "",
        "temperature": 0.3,
        "max_tokens": 256,
    }
    return mgr, settings, state


def collect(mgr, settings, messages=None, stop=None):
    stop = stop or threading.Event()
    msgs = messages or [{"role": "user", "content": "тест"}]
    return list(mgr.stream_chat(settings, msgs, {}, stop))


# ============================================================
# Тесты
# ============================================================

def test_ping_and_models(server):
    url, _ = server
    client = llm_online.FreeLLMAPIClient(url)
    assert client.ping() is True
    models = client.list_models()
    ids = {m["id"] for m in models}
    assert {"gemma-4b", "qwen3-coder-480b-a22b", "deepseek-v4-pro"} <= ids


def test_auto_picks_strongest(manager):
    mgr, settings, state = manager
    events = collect(mgr, settings)
    used = [e for e in events if e[0] == "model"]
    assert used[0][1] == "qwen3-coder-480b-a22b"  # самая мощная по рейтингу
    tokens = "".join(e[1] for e in events if e[0] == "token")
    assert "qwen3-coder-480b-a22b" in tokens
    assert state.chat_calls[0][0] == "qwen3-coder-480b-a22b"


def test_fallback_on_503(manager):
    mgr, settings, state = manager
    state.set_fail("qwen3-coder-480b-a22b", 503, 1)
    events = collect(mgr, settings)
    used = [e[1] for e in events if e[0] == "model"]
    assert used[0] == "qwen3-coder-480b-a22b"
    assert used[1] != used[0]  # автопереключение
    tokens = "".join(e[1] for e in events if e[0] == "token")
    assert tokens  # пользователь получил ответ, а не ошибку
    end = [e for e in events if e[0] == "end"]
    assert end and end[0][1] == used[-1]


def test_fallback_on_429_with_retry_after(manager):
    mgr, settings, state = manager
    state.retry_after = "1"
    state.set_fail("qwen3-coder-480b-a22b", 429, 1)
    events = collect(mgr, settings)
    used = [e[1] for e in events if e[0] == "model"]
    assert len(used) >= 2
    health = mgr.selector(settings).health
    retry_in = health.retry_in("qwen3-coder-480b-a22b")
    assert 0 < retry_in <= 2.0  # учтён Retry-After


def test_fallback_exhausted_message_not_traceback(manager):
    mgr, settings, state = manager
    for m in MODELS:
        state.set_fail(m["id"], 503, 10)
    with pytest.raises(llm_online.OnlineError) as ei:
        collect(mgr, settings)
    msg = ei.value.message
    assert "перебраны" in msg.lower() or "недоступны" in msg.lower() or "нет доступных" in msg.lower()
    assert ei.value.retryable is True
    assert "Traceback" not in msg


def test_no_models_at_all(manager):
    mgr, settings, state = manager
    state.models_available = False
    with pytest.raises(llm_online.OnlineError):
        collect(mgr, settings)


def test_recovery_returns_to_strongest(manager):
    mgr, settings, state = manager
    # 1) мощная модель падает → fallback на вторую
    state.set_fail("qwen3-coder-480b-a22b", 503, 1)
    ev1 = collect(mgr, settings)
    first_used = [e[1] for e in ev1 if e[0] == "model"]
    assert first_used[0] == "qwen3-coder-480b-a22b"
    assert len(first_used) == 2
    # 2) cooldown активен → AUTO использует следующую по силе
    ev2 = collect(mgr, settings)
    used2 = [e[1] for e in ev2 if e[0] == "model"][0]
    assert used2 != "qwen3-coder-480b-a22b"
    # 3) cooldown истёк → автоматический возврат к мощной модели
    sel = mgr.selector(settings)
    with llm_online._lock:
        sel.health._cooldown_until.pop("qwen3-coder-480b-a22b", None)
        sel.health._failures.pop("qwen3-coder-480b-a22b", None)
    ev3 = collect(mgr, settings)
    used3 = [e[1] for e in ev3 if e[0] == "model"][0]
    assert used3 == "qwen3-coder-480b-a22b"


def test_manual_selection_with_fallback(manager):
    mgr, settings, state = manager
    settings["model_mode"] = "manual"
    settings["online_model"] = "deepseek-v4-pro"
    events = collect(mgr, settings)
    used = [e[1] for e in events if e[0] == "model"]
    assert used[0] == "deepseek-v4-pro"
    # выбранная упала → fallback на другую
    state.set_fail("deepseek-v4-pro", 503, 1)
    events2 = collect(mgr, settings)
    used2 = [e[1] for e in events2 if e[0] == "model"]
    assert used2[0] == "deepseek-v4-pro"
    assert len(used2) == 2
    assert used2[1] != used2[0]


def test_context_truncation_under_limit(manager):
    mgr, settings, state = manager
    big = "Документ: " + ("данные " * 60000)  # заведомо > лимита запроса
    msgs = [
        {"role": "system", "content": big},
        {"role": "user", "content": "вопрос"},
    ]
    collect(mgr, settings, msgs)
    sent = state.chat_calls[-1][1]
    size = len(json.dumps(sent, ensure_ascii=False).encode("utf-8"))
    assert size <= llm_online.FREELLMAPI_MAX_REQUEST_BYTES
    assert "контекст сокращён" in sent["messages"][0]["content"]


def test_same_rag_context_for_all_models(manager):
    """Все модели получают один и тот же RAG-контекст — индекс не зависит от модели."""
    mgr, settings, state = manager
    rag_msgs = [
        {"role": "system", "content": "Фрагмент из 027-КЖ.pdf, стр. 14: бетон B25."},
        {"role": "user", "content": "Какой бетон в 027-КЖ?"},
    ]
    state.set_fail("qwen3-coder-480b-a22b", 503, 1)
    collect(mgr, settings, rag_msgs)
    calls = state.chat_calls
    assert len(calls) == 2
    assert calls[0][1]["messages"] == calls[1][1]["messages"]


def test_api_key_masked_never_leaks(manager, caplog):
    mgr, settings, state = manager
    secret = "sk-super-secret-key-abcd1234"
    settings["freellmapi"]["api_key"] = secret
    with caplog.at_level("INFO"):
        collect(mgr, settings)
    status = mgr.status(settings)
    assert secret not in json.dumps(status)
    assert status["key_configured"] is True
    assert status["key_masked"].endswith("1234")
    assert secret not in caplog.text


def test_registry_cache_survives_outage(server, tmp_path, monkeypatch):
    url, state = server
    cache = tmp_path / "models_cache.json"
    monkeypatch.setattr(llm_online, "REGISTRY_CACHE_FILE", cache)
    mgr1 = llm_online.ModelManager()
    mgr1._default_base = url
    settings = {"freellmapi": {"url": url}, "model_mode": "auto", "online_model": ""}
    reg = mgr1.registry(settings)
    reg.refresh(force=True)
    assert reg.count() >= 3 and reg.source == "api"
    # API «падает» — новый менеджер стартует с дискового кэша
    state.models_available = False
    mgr2 = llm_online.ModelManager()
    mgr2._default_base = url
    reg2 = mgr2.registry(settings)
    ok = reg2.refresh()
    assert ok is False
    assert reg2.count() >= 3
    assert reg2.source == "cache"
    state.models_available = True


def test_status_endpoint_shape(manager):
    mgr, settings, state = manager
    st = mgr.status(settings)
    assert st["connected"] is True
    assert st["models_count"] == len(MODELS)
    assert st["active_model"] == "qwen3-coder-480b-a22b"
    assert st["models"][0]["score"] >= st["models"][-1]["score"]
    assert all("name" in m for m in st["models"])


def test_ranker_prefers_big_context_reasoning_coding():
    score_big, _ = llm_online.rank_model({"id": "qwen3-coder-480b-a22b"})
    score_small, _ = llm_online.rank_model({"id": "gemma-4b-it"})
    assert score_big > score_small
    _, flags = llm_online.rank_model({"id": "qwq-32b"})
    assert flags.get("reasoning")
    _, flags2 = llm_online.rank_model({"id": "starcoder2-15b"})
    assert flags2.get("coding")


def test_stop_event_cancels_stream(manager):
    mgr, settings, state = manager
    stop = threading.Event()
    stop.set()
    events = collect(mgr, settings, stop=stop)
    assert not [e for e in events if e[0] == "token"]
