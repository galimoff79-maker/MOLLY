from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

from config import (
    PROJECTS_DATA_DIR,
    SQLITE_BUSY_BACKOFF,
    SQLITE_BUSY_RETRIES,
    SQLITE_PRAGMAS,
    SQLITE_TIMEOUT,
)
from logging_setup import get_logger

logger = get_logger("database")

T = TypeVar("T")


# ============================================================
# Molly PTO — SQLite Database
# ============================================================


# ============================================================
# FTS5
# ============================================================
#
# Таблица contentless (content=''): строки из неё нельзя удалять
# обычным DELETE. Удаление — только специальной командой 'delete'
# с теми же значениями, что были вставлены (см. fts_delete_document).

FTS_COLUMNS = (
    "filename",
    "full_path",
    "section",
    "document_type",
    "category",
    "document_number",
    "work",
    "construction",
    "material",
    "text_content",
)

# ВАЖНО: у external-content таблицы (content='documents') SQLite при
# запросе сам подтягивает колонки из исходной таблицы, поэтому такие
# строки корректно работают и с bm25(), и с snippet().
#
# Раньше использовалась contentless-таблица (content=''): в неё можно
# было вставить токены, но прочитать обратно — нет (ошибка
# "unknown column"), из-за чего весь FTS-поиск молча падал в запасной
# LIKE-поиск и не находил ничего, чего нет в метаданных.
FTS_CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
USING fts5(
    filename,
    full_path,
    section,
    document_type,
    category,
    document_number,
    work,
    construction,
    material,
    text_content UNINDEXED,
    content='documents',
    content_rowid='id',
    tokenize='unicode61'
);
"""


def _sqlite_supports_delete_all(conn: sqlite3.Connection) -> bool:
    """
    Команда 'delete-all' появилась в SQLite 3.43.0 (2023-10).
    """

    parts = conn.execute("SELECT sqlite_version()").fetchone()[0].split(".")

    try:
        version = tuple(int(p) for p in parts[:3])
    except ValueError:
        return False

    if len(version) < 3:
        version = (*version, *(0,) * (3 - len(version)))

    return version >= (3, 43, 0)


def project_db_path(project_path: str | Path) -> Path:
    """
    Возвращает отдельную SQLite-базу для конкретного проекта.

    Разные проекты физически разделены и никогда не смешиваются.
    """

    normalized = str(Path(project_path).resolve()).lower()

    project_hash = hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()[:24]

    return PROJECTS_DATA_DIR / f"{project_hash}.sqlite3"


def _is_busy_error(exc: BaseException) -> bool:

    if not isinstance(exc, sqlite3.OperationalError):
        return False

    message = str(exc).lower()

    return (
        "locked" in message
        or "busy" in message
    )


def retry_on_busy(func: Callable[..., T]) -> Callable[..., T]:
    """
    Повторяет операцию при SQLITE_BUSY / "database is locked".

    Основную работу делает busy_timeout соединения (SQLITE_TIMEOUT),
    но он не покрывает часть случаев (например PRAGMA journal_mode=WAL
    и upgrade read->write транзакции), поэтому на записи добавлен retry
    с экспоненциальной паузой.
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:

        attempt = 0

        while True:

            try:
                return func(*args, **kwargs)

            except sqlite3.OperationalError as exc:

                if (
                    not _is_busy_error(exc)
                    or attempt >= SQLITE_BUSY_RETRIES
                ):
                    raise

                delay = SQLITE_BUSY_BACKOFF * (2 ** attempt)

                attempt += 1

                logger.warning(
                    "SQLite занята (%s), повтор %d/%d через %.2f с",
                    exc,
                    attempt,
                    SQLITE_BUSY_RETRIES,
                    delay,
                )

                time.sleep(delay)

    return wrapper


class MollyConnection(sqlite3.Connection):
    """
    sqlite3.Connection, который ЗАКРЫВАЕТСЯ при выходе из `with`.

    Штатный sqlite3 в `with conn:` только коммитит/откатывает
    транзакцию, но соединение не закрывает. На Windows незакрытые
    соединения держат файлы .sqlite3-wal / .sqlite3-shm.
    """

    def __exit__(self, exc_type, exc_value, traceback) -> bool:

        try:
            return bool(
                super().__exit__(
                    exc_type,
                    exc_value,
                    traceback,
                )
            )
        finally:
            self.close()


@retry_on_busy
def connect_db(project_path: str | Path) -> sqlite3.Connection:
    """
    Открывает SQLite-базу проекта.

    Использовать как `with connect_db(path) as conn:` — по выходу
    транзакция коммитится (или откатывается при ошибке),
    соединение закрывается.

    check_same_thread=False оставлен намеренно: соединения не
    разделяются между потоками, но FastAPI/to_thread может создать
    соединение в одном потоке, а закрыть (GC) в другом.
    """

    db_path = project_db_path(project_path)

    db_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = sqlite3.connect(
        str(db_path),
        timeout=SQLITE_TIMEOUT,
        check_same_thread=False,
        factory=MollyConnection,
    )

    try:

        conn.row_factory = sqlite3.Row

        conn.execute(
            f"PRAGMA busy_timeout={int(SQLITE_TIMEOUT * 1000)}"
        )

        for pragma, value in SQLITE_PRAGMAS.items():
            conn.execute(
                f"PRAGMA {pragma}={value}"
            )

    except BaseException:

        conn.close()

        raise

    return conn


_initialized: set[str] = set()

_init_lock = threading.Lock()


def _fts_table_sql(db_path: str | Path) -> str:
    """
    Возвращает SQL определения таблицы documents_fts из sqlite_master.
    Пустая строка — если таблицы нет.
    """

    conn = sqlite3.connect(str(db_path), timeout=SQLITE_TIMEOUT)

    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='documents_fts'"
        ).fetchone()
    finally:
        conn.close()

    return (row[0] or "") if row else ""


@retry_on_busy
def _migrate_fts_to_external_content(db_path: str | Path) -> bool:
    """
    Миграция старой contentless-схемы FTS (content='') на
    external-content (content='documents').

    У contentless-таблицы SQLite не умеет читать колонки обратно,
    поэтому любой запрос с bm25()/snippet() падал с ошибкой
    'unknown column', и весь полнотекстовый поиск молча
    скатывался в LIKE по метаданным — «МОЛЛИ не видит файлы».

    Миграция безопасна: старые данные FTS всё равно нельзя было
    прочитать, индекс пересобирается из documents + document_text.

    Возвращает True, если миграция была выполнена.
    """

    ddl = _fts_table_sql(db_path)

    # Таблицы нет — схема создаст её сама при необходимости.
    if not ddl:
        return False

    # Уже external-content или обычный external-content по умолчанию.
    normalized = " ".join(ddl.lower().split())

    if "content='documents'" in normalized or "content=\"documents\"" in normalized:
        return False

    logger.warning(
        "FTS: обнаружена устаревшая contentless-схема, выполняется "
        "миграция на external-content: %s",
        db_path,
    )

    with connect_db(db_path) as conn:

        conn.execute("DROP TABLE IF EXISTS documents_fts")

        conn.execute(FTS_CREATE_SQL.strip().rstrip(";"))

        conn.commit()

        count = _rebuild_fts_from_data(conn)

        conn.commit()

    logger.info(
        "FTS мигрирован и пересобран: %d документов",
        count,
    )

    return True


def _rebuild_fts_from_data(conn: sqlite3.Connection) -> int:
    """
    Заполняет external-content FTS-таблицу данными из
    documents + document_text. Возвращает число записей.
    """

    conn.execute(
        "INSERT INTO documents_fts (rowid, "
        + ", ".join(FTS_COLUMNS)
        + ") "
        "SELECT d.id, "
        "COALESCE(d.filename,''), COALESCE(d.full_path,''), "
        "COALESCE(d.section,''), COALESCE(d.document_type,''), "
        "COALESCE(d.category,''), COALESCE(d.document_number,''), "
        "COALESCE(d.work,''), COALESCE(d.construction,''), "
        "COALESCE(d.material,''), COALESCE(t.text_content,'') "
        "FROM documents d "
        "LEFT JOIN document_text t ON t.document_id = d.id"
    )

    row = conn.execute(
        "SELECT COUNT(*) FROM documents_fts"
    ).fetchone()

    return int(row[0]) if row else 0


def initialize_database(
    project_path: str | Path,
    force: bool = False,
) -> Path:
    """
    Создаёт структуру базы данных проекта.

    Схема применяется один раз за процесс для каждой БД
    (раньше она выполнялась на каждый GET /api/project).
    Если файл БД исчез — схема создаётся заново.
    """

    db_path = project_db_path(project_path)

    key = str(db_path)

    if (
        not force
        and key in _initialized
        and db_path.exists()
    ):
        return db_path

    with _init_lock:

        _create_schema(project_path)

        _migrate_fts_to_external_content(db_path)

        _initialized.add(key)

    logger.info(
        "База проекта готова: %s",
        db_path,
    )

    return db_path


@retry_on_busy
def _create_schema(project_path: str | Path) -> Path:

    db_path = project_db_path(project_path)

    with connect_db(project_path) as conn:

        conn.executescript(
            """
            ----------------------------------------------------
            -- Общая информация о проекте
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS project_info (
                id INTEGER PRIMARY KEY CHECK (id = 1),

                project_path TEXT NOT NULL,
                project_name TEXT,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );


            ----------------------------------------------------
            -- Документы
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                full_path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,

                extension TEXT,
                size_bytes INTEGER DEFAULT 0,
                mtime REAL DEFAULT 0,

                file_hash TEXT,

                section TEXT,
                object_name TEXT,

                document_type TEXT,
                category TEXT,
                stage TEXT,

                document_number TEXT,
                document_date TEXT,

                organization TEXT,
                author TEXT,
                performer TEXT,

                work TEXT,
                construction TEXT,
                material TEXT,

                executive INTEGER DEFAULT 0,
                project_document INTEGER DEFAULT 0,

                is_scanned INTEGER DEFAULT 0,
                parser_status TEXT DEFAULT 'unknown',

                classification_confidence REAL DEFAULT 0,

                classification_evidence TEXT,

                text_length INTEGER DEFAULT 0,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );


            CREATE INDEX IF NOT EXISTS idx_documents_filename
                ON documents(filename);

            CREATE INDEX IF NOT EXISTS idx_documents_extension
                ON documents(extension);

            CREATE INDEX IF NOT EXISTS idx_documents_section
                ON documents(section);

            CREATE INDEX IF NOT EXISTS idx_documents_document_type
                ON documents(document_type);

            CREATE INDEX IF NOT EXISTS idx_documents_category
                ON documents(category);

            CREATE INDEX IF NOT EXISTS idx_documents_document_number
                ON documents(document_number);

            CREATE INDEX IF NOT EXISTS idx_documents_mtime
                ON documents(mtime);


            ----------------------------------------------------
            -- Полный текст документов
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS document_text (
                document_id INTEGER PRIMARY KEY,

                text_content TEXT NOT NULL DEFAULT '',

                page_count INTEGER DEFAULT 0,

                parser_name TEXT,
                parser_version TEXT,

                FOREIGN KEY(document_id)
                    REFERENCES documents(id)
                    ON DELETE CASCADE
            );


            ----------------------------------------------------
            -- Отдельные страницы / листы / части документа
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS document_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                document_id INTEGER NOT NULL,

                part_type TEXT NOT NULL,
                part_number INTEGER,

                name TEXT,

                text_content TEXT NOT NULL DEFAULT '',

                metadata_json TEXT,

                FOREIGN KEY(document_id)
                    REFERENCES documents(id)
                    ON DELETE CASCADE
            );


            CREATE INDEX IF NOT EXISTS idx_document_parts_document
                ON document_parts(document_id);

            CREATE INDEX IF NOT EXISTS idx_document_parts_type
                ON document_parts(part_type);


            ----------------------------------------------------
            -- Разделы проекта
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                normalized_name TEXT NOT NULL UNIQUE,

                display_name TEXT,

                first_seen_path TEXT,

                document_count INTEGER DEFAULT 0,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );


            ----------------------------------------------------
            -- Типы документов
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS document_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                code TEXT NOT NULL UNIQUE,

                name TEXT NOT NULL,

                category TEXT,

                description TEXT,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );


            ----------------------------------------------------
            -- Связи между документами
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS document_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                source_document_id INTEGER NOT NULL,
                target_document_id INTEGER NOT NULL,

                relation_type TEXT NOT NULL,

                confidence REAL DEFAULT 0,

                evidence TEXT,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                UNIQUE (
                    source_document_id,
                    target_document_id,
                    relation_type
                ),

                FOREIGN KEY(source_document_id)
                    REFERENCES documents(id)
                    ON DELETE CASCADE,

                FOREIGN KEY(target_document_id)
                    REFERENCES documents(id)
                    ON DELETE CASCADE
            );


            CREATE INDEX IF NOT EXISTS idx_relations_source
                ON document_relations(source_document_id);

            CREATE INDEX IF NOT EXISTS idx_relations_target
                ON document_relations(target_document_id);

            CREATE INDEX IF NOT EXISTS idx_relations_type
                ON document_relations(relation_type);


            ----------------------------------------------------
            -- Конфликты классификации
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS document_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                document_id INTEGER NOT NULL,

                field_name TEXT NOT NULL,

                value_from_path TEXT,
                value_from_filename TEXT,
                value_from_content TEXT,

                description TEXT,

                resolved INTEGER DEFAULT 0,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(document_id)
                    REFERENCES documents(id)
                    ON DELETE CASCADE
            );


            CREATE INDEX IF NOT EXISTS idx_conflicts_document
                ON document_conflicts(document_id);


            ----------------------------------------------------
            -- Состояние индексации
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS index_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),

                status TEXT DEFAULT 'idle',

                started_at TEXT,
                finished_at TEXT,

                scanned INTEGER DEFAULT 0,
                indexed INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                removed INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,

                total_files INTEGER DEFAULT 0,

                current_file TEXT,

                error_message TEXT
            );


            ----------------------------------------------------
            -- Структура проекта
            ----------------------------------------------------

            CREATE TABLE IF NOT EXISTS project_structure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                parent_path TEXT,

                path TEXT NOT NULL UNIQUE,

                name TEXT NOT NULL,

                item_type TEXT NOT NULL,

                extension TEXT,

                depth INTEGER DEFAULT 0,

                document_count INTEGER DEFAULT 0,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );


            CREATE INDEX IF NOT EXISTS idx_structure_parent
                ON project_structure(parent_path);

            CREATE INDEX IF NOT EXISTS idx_structure_type
                ON project_structure(item_type);


            """
            + FTS_CREATE_SQL
            + """

            ----------------------------------------------------
            -- Заполняем служебные типы документов
            ----------------------------------------------------

            INSERT OR IGNORE INTO document_types
                (code, name, category, description)
            VALUES
                ('АОСР', 'Акт освидетельствования скрытых работ',
                 'executive',
                 'Акт освидетельствования скрытых работ'),

                ('АОК', 'Акт освидетельствования конструкций',
                 'executive',
                 'Акт освидетельствования ответственных конструкций'),

                ('КС-2', 'Акт о приемке выполненных работ',
                 'cost',
                 'Форма КС-2'),

                ('КС-3', 'Справка о стоимости выполненных работ',
                 'cost',
                 'Форма КС-3'),

                ('КС-6', 'Общий журнал работ',
                 'cost',
                 'Журнал учета выполненных работ'),

                ('КС-6а', 'Журнал учета выполненных работ',
                 'cost',
                 'Форма КС-6а'),

                ('ЭЛЕКТРОННЫЙ ЖУРНАЛ',
                 'Электронный журнал',
                 'executive',
                 'Электронный журнал производства работ'),

                ('ИСПОЛНИТЕЛЬНАЯ СХЕМА',
                 'Исполнительная схема',
                 'executive',
                 'Исполнительная документация'),

                ('ИСПОЛНИТЕЛЬНЫЙ ЧЕРТЕЖ',
                 'Исполнительный чертеж',
                 'executive',
                 'Исполнительный чертеж'),

                ('ПРОТОКОЛ',
                 'Протокол',
                 'executive',
                 'Протокол испытаний / контроля'),

                ('ПАСПОРТ',
                 'Паспорт',
                 'executive',
                 'Паспорт материала или оборудования'),

                ('СЕРТИФИКАТ',
                 'Сертификат',
                 'executive',
                 'Сертификат качества / соответствия'),

                ('ПИСЬМО',
                 'Письмо',
                 'correspondence',
                 'Входящее или исходящее письмо'),

                ('ТЕХНИЧЕСКОЕ РЕШЕНИЕ',
                 'Техническое решение',
                 'correspondence',
                 'Техническое решение'),

                ('РД',
                 'Рабочая документация',
                 'project',
                 'Рабочая документация'),

                ('ПД',
                 'Проектная документация',
                 'project',
                 'Проектная документация'),

                ('СПЕЦИФИКАЦИЯ',
                 'Спецификация',
                 'project',
                 'Спецификация'),

                ('ДОГОВОР',
                 'Договор',
                 'general',
                 'Договор / контракт'),

                ('МСГ',
                 'Месячно-суточный график',
                 'general',
                 'МСГ'),

                ('ДРУГОЕ',
                 'Другой документ',
                 'general',
                 'Документ другого типа');
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO index_status (
                id,
                status
            )
            VALUES (
                1,
                'idle'
            )
            """
        )

        conn.execute(
            """
            INSERT INTO project_info (
                id,
                project_path,
                project_name
            )
            VALUES (
                1,
                ?,
                ?
            )
            ON CONFLICT(id)
            DO UPDATE SET
                project_path = excluded.project_path,
                project_name = excluded.project_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                str(Path(project_path).resolve()),
                Path(project_path).name,
            ),
        )

        conn.commit()

    return db_path


# ============================================================
# Helpers
# ============================================================

@retry_on_busy
def execute(
    project_path: str | Path,
    sql: str,
    params: tuple[Any, ...] = (),
) -> None:
    with connect_db(project_path) as conn:
        conn.execute(sql, params)
        conn.commit()


def fetch_one(
    project_path: str | Path,
    sql: str,
    params: tuple[Any, ...] = (),
) -> sqlite3.Row | None:

    with connect_db(project_path) as conn:
        return conn.execute(
            sql,
            params,
        ).fetchone()


def fetch_all(
    project_path: str | Path,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[sqlite3.Row]:

    with connect_db(project_path) as conn:
        return conn.execute(
            sql,
            params,
        ).fetchall()


# ============================================================
# Document lookup
# ============================================================

def get_document_by_path(
    project_path: str | Path,
    full_path: str | Path,
) -> sqlite3.Row | None:

    return fetch_one(
        project_path,
        """
        SELECT *
        FROM documents
        WHERE full_path = ?
        """,
        (
            str(Path(full_path).resolve()),
        ),
    )


def get_document(
    project_path: str | Path,
    document_id: int,
) -> sqlite3.Row | None:

    return fetch_one(
        project_path,
        """
        SELECT *
        FROM documents
        WHERE id = ?
        """,
        (document_id,),
    )


# ============================================================
# Index status
# ============================================================

@retry_on_busy
def update_index_status(
    project_path: str | Path,
    **values: Any,
) -> None:

    if not values:
        return

    allowed = {
        "status",
        "started_at",
        "finished_at",
        "scanned",
        "indexed",
        "skipped",
        "removed",
        "errors",
        "total_files",
        "current_file",
        "error_message",
    }

    values = {
        key: value
        for key, value in values.items()
        if key in allowed
    }

    if not values:
        return

    fields = ", ".join(
        f"{key} = ?"
        for key in values
    )

    params = tuple(values.values())

    with connect_db(project_path) as conn:
        conn.execute(
            f"""
            UPDATE index_status
            SET {fields}
            WHERE id = 1
            """,
            params,
        )

        conn.commit()


def get_index_status(
    project_path: str | Path,
) -> dict[str, Any]:

    row = fetch_one(
        project_path,
        """
        SELECT *
        FROM index_status
        WHERE id = 1
        """,
    )

    if row is None:
        return {
            "status": "idle",
            "scanned": 0,
            "indexed": 0,
            "skipped": 0,
            "removed": 0,
            "errors": 0,
            "total_files": 0,
            "current_file": None,
            "error_message": None,
        }

    return dict(row)


# ============================================================
# FTS maintenance
# ============================================================

# Таблица внешнесодержимая (content='documents'), поэтому в неё
# НЕЛЬЗЯ вставлять значения напрямую — вместо этого строка индекса
# пересобирается командой 'replace' из актуальных данных documents
# и document_text. Удаление — команда 'delete' с теми же значениями,
# что были проиндексированы (её тоже берём из БД ДО изменений).

_FTS_SELECT_SQL = """
SELECT
    d.filename, d.full_path, d.section, d.document_type,
    d.category, d.document_number, d.work, d.construction,
    d.material,
    t.text_content AS text_content
FROM documents d
LEFT JOIN document_text t
    ON t.document_id = d.id
WHERE d.id = ?
"""


def _fts_values(
    row: sqlite3.Row,
    text: str,
) -> tuple[str, ...]:
    """
    Значения колонок FTS в зафиксированном порядке (None -> "").
    """

    return (
        row["filename"] or "",
        row["full_path"] or "",
        row["section"] or "",
        row["document_type"] or "",
        row["category"] or "",
        row["document_number"] or "",
        row["work"] or "",
        row["construction"] or "",
        row["material"] or "",
        text or "",
    )


def fts_sync_document(
    conn: sqlite3.Connection,
    document_id: int,
) -> bool:
    """
    Синхронизирует индекс FTS с текущим содержимым документов
    (documents + document_text). Вызывать ПОСЛЕ записи/обновления
    строки документа в той же транзакции.

    Возвращает True, если документ найден в БД.
    """

    row = conn.execute(_FTS_SELECT_SQL, (document_id,)).fetchone()

    if row is None:
        return False

    conn.execute(
        "INSERT INTO documents_fts (documents_fts, rowid, "
        + ", ".join(FTS_COLUMNS)
        + ") VALUES ('replace', ?"
        + ", ?" * len(FTS_COLUMNS)
        + ")",
        (
            document_id,
            *_fts_values(row, row["text_content"] or ""),
        ),
    )

    return True


def fts_delete_document(
    conn: sqlite3.Connection,
    document_id: int,
) -> bool:
    """
    Убирает документ из FTS-индекса.

    Вызывать ДО удаления/изменения строк documents и document_text:
    для таблицы content='documents' команда 'delete' должна получить
    РОВНО те значения, которые были проиндексированы.

    Возвращает True, если запись была удалена.
    """

    row = conn.execute(_FTS_SELECT_SQL, (document_id,)).fetchone()

    if row is None:
        return False

    conn.execute(
        "INSERT INTO documents_fts (documents_fts, rowid, "
        + ", ".join(FTS_COLUMNS)
        + ") VALUES ('delete', ?"
        + ", ?" * len(FTS_COLUMNS)
        + ")",
        (
            document_id,
            *_fts_values(row, row["text_content"] or ""),
        ),
    )

    return True


@retry_on_busy
def rebuild_fts(project_path: str | Path) -> int:
    """
    Полная пересборка FTS-индекса из documents + document_text.
    Схема не меняется. Нужна, если индекс разошёлся с данными
    (например, после старых версий, где FTS не обновлялся при
    повторной индексации).

    Возвращает количество документов в индексе.
    """

    initialize_database(project_path)

    with connect_db(project_path) as conn:

        # Проверяем актуальность схемы (на случай, если миграция
        # ещё не выполнялась для этой БД в этом процессе).
        ddl = " ".join(
            (
                conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='documents_fts'"
                ).fetchone() or ("",)
            )[0].lower().split()
        )

        if "content='documents'" not in ddl and 'content="documents"' not in ddl:
            # Устаревшая contentless-схема — пересоздаём таблицу.
            conn.execute("DROP TABLE IF EXISTS documents_fts")
            conn.execute(FTS_CREATE_SQL.strip().rstrip(";"))
            conn.commit()

        try:
            conn.execute(
                "INSERT INTO documents_fts(documents_fts) "
                "VALUES('rebuild')"
            )
            count_row = conn.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()
            count = int(count_row[0]) if count_row else 0
        except sqlite3.OperationalError:
            # 'rebuild' недоступен — заполняем индекс напрямую.
            conn.execute("DELETE FROM documents_fts")
            count = _rebuild_fts_from_data(conn)

        conn.commit()

    logger.info(
        "FTS пересобран: %d документов",
        count,
    )

    return count


@retry_on_busy
def mark_stale_indexing(project_path: str | Path) -> bool:
    """
    Если процесс упал во время индексации, в БД остаётся
    status='indexing'. При старте переводим его в 'interrupted'.
    """

    with connect_db(project_path) as conn:

        cursor = conn.execute(
            """
            UPDATE index_status
            SET status = 'interrupted'
            WHERE id = 1
              AND status = 'indexing'
            """
        )

        conn.commit()

        return cursor.rowcount > 0
