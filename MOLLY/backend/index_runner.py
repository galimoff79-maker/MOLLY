from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import settings_store
from indexing.indexer import ProjectIndexer
from logging_setup import get_logger

logger = get_logger("index_runner")

# ============================================================
# Запуск индексации в фоновом потоке
# ============================================================
#
# Раньше состояние жило в asyncio.Task конкретного event loop. Теперь,
# когда приложение обслуживают два сервера (локальный и LAN, у каждого
# свой loop), состояние общее и потокобезопасное.

_lock = threading.Lock()

_indexer: ProjectIndexer | None = None
_thread: threading.Thread | None = None
_project: Path | None = None

last_finished: float = 0.0
last_result: dict[str, Any] | None = None


def is_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


def start(project: str | Path, reason: str = "manual") -> bool:
    """Запускает индексацию. False, если она уже идёт."""

    global _thread, _project

    with _lock:

        if _thread is not None and _thread.is_alive():
            return False

        _project = Path(project)

        _thread = threading.Thread(
            target=_work,
            args=(Path(project), reason),
            name="molly-indexer",
            daemon=True,
        )

        _thread.start()

    return True


def _work(project: Path, reason: str) -> None:

    global _indexer, _thread, last_finished, last_result

    try:

        logger.info("Индексация (%s): %s", reason, project)

        indexer = ProjectIndexer(
            project,
            exclude=settings_store.get()["excluded"],
        )

        with _lock:
            _indexer = indexer

        last_result = indexer.run()

    except Exception:

        logger.exception("Индексация завершилась с ошибкой")

    finally:

        with _lock:
            _indexer = None
            last_finished = time.monotonic()


def stop() -> bool:

    with _lock:
        indexer = _indexer

    if indexer is None:
        return False

    indexer.stop()

    return True


def snapshot(project: str | Path) -> dict[str, Any] | None:
    """Живой статус индексатора (из памяти), если он работает над этим проектом."""

    with _lock:

        if (
            _indexer is None
            or _thread is None
            or not _thread.is_alive()
            or _project is None
        ):
            return None

        try:
            same = _project.resolve() == Path(project).resolve()
        except OSError:
            same = False

        return _indexer.snapshot() if same else None


def shutdown(timeout: float = 15.0) -> None:

    stop()

    with _lock:
        thread = _thread

    if thread is not None and thread.is_alive():
        thread.join(timeout)

        if thread.is_alive():
            logger.warning("Индексация не остановилась за %.0f с", timeout)


# ------------------------------------------------------------
# Автообновление индекса
# ------------------------------------------------------------

def scheduler_loop(
    get_project,
    stop_event: threading.Event,
    tick: float = 15.0,
) -> None:
    """
    Периодически запускает инкрементальную индексацию: находит новые,
    изменённые и удалённые файлы. Интервал — в настройках
    («Автообновление, минут», 0 = выключено).
    """

    # Сразу после старта — один быстрый проход, чтобы подхватить
    # изменения, сделанные, пока МОЛЛИ была выключена.
    first = True

    while not stop_event.wait(5.0 if first else tick):

        try:

            project = get_project()

            if project is None or is_running():
                continue

            minutes = settings_store.get()["auto_reindex_minutes"]

            if minutes <= 0:
                first = False
                continue

            due = (
                first
                or last_finished == 0.0
                or time.monotonic() - last_finished >= minutes * 60
            )

            first = False

            if due:
                start(project, "auto")

        except Exception:
            logger.exception("Ошибка планировщика индексации")
