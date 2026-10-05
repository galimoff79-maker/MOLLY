from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    INDEX_BATCH_SIZE,
    INDEX_PROGRESS_INTERVAL,
    MAX_DOCUMENT_TEXT,
)
from database import (
    connect_db,
    fts_delete_document,
    fts_sync_document,
    get_document_by_path,
    initialize_database,
    update_index_status,
)
from indexing.analyzer import analyze_document
from indexing.extractors import (
    ExtractedDocument,
    extract_document,
)
from indexing.scanner import (
    ExcludeRules,
    ScannedFile,
    scan_project,
)
from logging_setup import get_logger

logger = get_logger("indexer")


# ============================================================
# Molly PTO — Main Indexer
# ============================================================


class ProjectIndexer:
    """
    Полный индексатор проекта.

    Pipeline:

        scanner
           ↓
        extractor
           ↓
        analyzer
           ↓
        SQLite
           ↓
        FTS

    Исходные файлы проекта НЕ изменяются.
    """

    def __init__(
        self,
        project_path: str | Path,
        exclude: list[str] | None = None,
    ) -> None:

        self.project_path = Path(
            project_path
        ).resolve()

        self.exclude: list[str] = list(exclude or [])

        self.stop_event = threading.Event()

        self._lock = threading.Lock()

        self.status = {
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

        # Состояние одного прогона (сбрасывается в run()).
        #
        # _known — {full_path: (size, mtime, parser_status, file_hash)}
        #   из БД, загружается ОДНИМ запросом (вместо запроса на
        #   каждый файл).
        # _structure_seen — папки, уже записанные в project_structure
        #   (вместо UPSERT всех родителей на каждый файл).
        self._known: dict[str, tuple[int, float, str, str]] | None = None

        self._structure_seen: set[str] = set()

        self._last_flush: float = time.monotonic()

        self._since_flush: int = 0

        initialize_database(
            self.project_path
        )

    # ========================================================
    # Status snapshot (для API: читается из event loop,
    # пишется из рабочего потока)
    # ========================================================

    def snapshot(self) -> dict[str, Any]:

        return dict(
            self.status
        )

    # ========================================================
    # Управление
    # ========================================================

    def stop(self) -> None:
        """
        Просит текущую индексацию остановиться.
        """

        self.stop_event.set()

    def reset_stop(self) -> None:
        self.stop_event.clear()

    # ========================================================
    # Hash
    # ========================================================

    @staticmethod
    def _file_hash(
        path: Path,
    ) -> str:

        """
        SHA-256 файла.

        Используется только при необходимости.
        Для больших файлов читается потоково.
        """

        digest = hashlib.sha256()

        with path.open(
            "rb"
        ) as file:

            while True:

                chunk = file.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                digest.update(
                    chunk
                )

        return digest.hexdigest()

    # ========================================================
    # Time
    # ========================================================

    @staticmethod
    def _now() -> str:

        return datetime.now(
            timezone.utc
        ).isoformat()

    # ========================================================
    # Document unchanged?
    # ========================================================

    def _is_unchanged(
        self,
        scanned: ScannedFile,
    ) -> bool:

        key = str(scanned.path)

        old_hash = ""

        if self._known is not None:

            known = self._known.get(key)

            if known is None:
                return False

            old_size, old_mtime, old_status = known[:3]

            old_hash = known[3] if len(known) > 3 else ""

        else:

            row = get_document_by_path(
                self.project_path,
                scanned.path,
            )

            if row is None:
                return False

            old_size = int(
                row["size_bytes"] or 0
            )

            old_mtime = float(
                row["mtime"] or 0
            )

            old_status = str(
                row["parser_status"] or ""
            )

            try:
                old_hash = str(row["file_hash"] or "")
            except (IndexError, KeyError):
                old_hash = ""

        # Файл, который в прошлый раз не удалось прочитать
        # (например, был открыт/заблокирован в Excel на Windows),
        # нужно пробовать снова, даже если размер и время те же.
        if old_status == "error":
            return False

        size_matches = old_size == scanned.size_bytes

        mtime_matches = abs(
            old_mtime - scanned.mtime
        ) < 0.0001

        # Быстрый путь: размер и время совпадают.
        if size_matches and mtime_matches:

            # Если при прошлой индексации хэш не сохранился
            # (старые базы) — доверяем размеру и времени.
            if not old_hash:
                return True

            # Надёжная проверка: файл мог быть отредактирован
            # без изменения размера (или с восстановленным mtime).
            try:
                new_hash = self._file_hash(scanned.path)
            except Exception as exc:
                logger.warning(
                    "Не удалось вычислить хэш %s: %s",
                    key,
                    exc,
                )
                return False

            return new_hash == old_hash

        # Время изменилось, а размер нет (или наоборот) —
        # сравниваем хэши, чтобы не переиндексировать зря.
        if size_matches and old_hash:

            try:
                new_hash = self._file_hash(scanned.path)
            except Exception as exc:
                logger.warning(
                    "Не удалось вычислить хэш %s: %s",
                    key,
                    exc,
                )
                return False

            return new_hash == old_hash

        return False

    def _load_known(
        self,
    ) -> dict[str, tuple[int, float, str, str]]:

        known: dict[str, tuple[int, float, str, str]] = {}

        with connect_db(
            self.project_path
        ) as conn:

            for row in conn.execute(
                """
                SELECT
                    full_path,
                    size_bytes,
                    mtime,
                    parser_status,
                    file_hash
                FROM documents
                """
            ):

                known[str(row["full_path"])] = (
                    int(row["size_bytes"] or 0),
                    float(row["mtime"] or 0),
                    str(row["parser_status"] or ""),
                    str(row["file_hash"] or ""),
                )

        return known

    # ========================================================
    # Text limit
    # ========================================================

    @staticmethod
    def _limit_text(
        text: str,
    ) -> str:

        if not text:
            return ""

        if len(text) <= MAX_DOCUMENT_TEXT:
            return text

        return (
            text[:MAX_DOCUMENT_TEXT]
            + "\n\n"
            "[Текст документа сокращён "
            "при индексации.]"
        )

    # ========================================================
    # JSON
    # ========================================================

    @staticmethod
    def _json(
        value: Any,
    ) -> str:

        try:

            return json.dumps(
                value,
                ensure_ascii=False,
                default=str,
            )

        except Exception:

            return "{}"

    # ========================================================
    # Insert/update document
    # ========================================================

    def _save_document(
        self,
        scanned: ScannedFile,
        extracted: ExtractedDocument,
    ) -> int:

        path = scanned.path

        text = self._limit_text(
            extracted.text
        )

        analysis = analyze_document(
            path,
            text,
        )

        file_hash = ""

        try:
            file_hash = self._file_hash(
                path
            )
        except Exception:
            # Hash не должен ломать индексацию.
            file_hash = ""

        evidence_json = self._json(
            [
                {
                    "source": evidence.source,
                    "value": evidence.value,
                    "weight": evidence.weight,
                    "description": evidence.description,
                }
                for evidence in analysis.evidence
            ]
        )

        # ----------------------------------------------------
        # DB
        # ----------------------------------------------------

        with connect_db(
            self.project_path
        ) as conn:

            existing = conn.execute(
                """
                SELECT id
                FROM documents
                WHERE full_path = ?
                """,
                (
                    str(path),
                ),
            ).fetchone()

            if existing:

                document_id = int(
                    existing["id"]
                )

                # Contentless FTS: старую запись нужно убрать
                # командой 'delete' ДО обновления documents и
                # document_text (значения берутся оттуда).
                fts_delete_document(
                    conn,
                    document_id,
                )

                conn.execute(
                    """
                    UPDATE documents
                    SET
                        filename = ?,
                        extension = ?,
                        size_bytes = ?,
                        mtime = ?,
                        file_hash = ?,

                        section = ?,
                        object_name = ?,

                        document_type = ?,
                        category = ?,
                        stage = ?,

                        document_number = ?,
                        document_date = ?,

                        organization = ?,
                        author = ?,
                        performer = ?,

                        work = ?,
                        construction = ?,
                        material = ?,

                        executive = ?,
                        project_document = ?,

                        is_scanned = ?,
                        parser_status = ?,

                        classification_confidence = ?,
                        classification_evidence = ?,

                        text_length = ?,

                        updated_at = CURRENT_TIMESTAMP

                    WHERE id = ?
                    """,
                    (
                        scanned.filename,
                        scanned.extension,
                        scanned.size_bytes,
                        scanned.mtime,
                        file_hash,

                        analysis.section,
                        analysis.object_name,

                        analysis.document_type,
                        analysis.category,
                        analysis.stage,

                        analysis.document_number,
                        analysis.document_date,

                        analysis.organization,
                        analysis.author,
                        analysis.performer,

                        analysis.work,
                        analysis.construction,
                        analysis.material,

                        1 if analysis.executive else 0,
                        (
                            1
                            if analysis.project_document
                            else 0
                        ),

                        1 if extracted.is_scanned else 0,
                        extracted.status,

                        analysis.confidence,
                        evidence_json,

                        len(text),

                        document_id,
                    ),
                )

            else:

                cursor = conn.execute(
                    """
                    INSERT INTO documents (
                        full_path,
                        filename,
                        extension,
                        size_bytes,
                        mtime,
                        file_hash,

                        section,
                        object_name,

                        document_type,
                        category,
                        stage,

                        document_number,
                        document_date,

                        organization,
                        author,
                        performer,

                        work,
                        construction,
                        material,

                        executive,
                        project_document,

                        is_scanned,
                        parser_status,

                        classification_confidence,
                        classification_evidence,

                        text_length
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?,
                        ?
                    )
                    """,
                    (
                        str(path),
                        scanned.filename,
                        scanned.extension,
                        scanned.size_bytes,
                        scanned.mtime,
                        file_hash,

                        analysis.section,
                        analysis.object_name,

                        analysis.document_type,
                        analysis.category,
                        analysis.stage,

                        analysis.document_number,
                        analysis.document_date,

                        analysis.organization,
                        analysis.author,
                        analysis.performer,

                        analysis.work,
                        analysis.construction,
                        analysis.material,

                        1 if analysis.executive else 0,
                        (
                            1
                            if analysis.project_document
                            else 0
                        ),

                        1 if extracted.is_scanned else 0,
                        extracted.status,

                        analysis.confidence,
                        evidence_json,

                        len(text),
                    ),
                )

                document_id = int(
                    cursor.lastrowid
                )

            # ------------------------------------------------
            # Full text
            # ------------------------------------------------

            conn.execute(
                """
                INSERT INTO document_text (
                    document_id,
                    text_content,
                    page_count,
                    parser_name,
                    parser_version
                )
                VALUES (?, ?, ?, ?, ?)

                ON CONFLICT(document_id)
                DO UPDATE SET
                    text_content = excluded.text_content,
                    page_count = excluded.page_count,
                    parser_name = excluded.parser_name,
                    parser_version = excluded.parser_version
                """,
                (
                    document_id,
                    text,
                    extracted.page_count,
                    extracted.parser_name,
                    extracted.parser_version,
                ),
            )

            # ------------------------------------------------
            # Parts
            # ------------------------------------------------

            conn.execute(
                """
                DELETE FROM document_parts
                WHERE document_id = ?
                """,
                (
                    document_id,
                ),
            )

            for part in extracted.parts:

                metadata_json = self._json(
                    part.metadata
                )

                conn.execute(
                    """
                    INSERT INTO document_parts (
                        document_id,
                        part_type,
                        part_number,
                        name,
                        text_content,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        part.part_type,
                        part.part_number,
                        part.name,
                        self._limit_text(
                            part.text
                        ),
                        metadata_json,
                    ),
                )

            # ------------------------------------------------
            # Conflicts
            # ------------------------------------------------

            conn.execute(
                """
                DELETE FROM document_conflicts
                WHERE document_id = ?
                """,
                (
                    document_id,
                ),
            )

            for conflict in analysis.conflicts:

                sources_json = self._json(
                    conflict.get(
                        "sources",
                        {},
                    )
                )

                conn.execute(
                    """
                    INSERT INTO document_conflicts (
                        document_id,
                        field_name,
                        description,
                        value_from_path,
                        value_from_filename,
                        value_from_content
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        conflict.get(
                            "field",
                            "",
                        ),
                        conflict.get(
                            "description",
                            "",
                        ),
                        self._json(
                            conflict
                            .get(
                                "sources",
                                {}
                            )
                            .get(
                                "path",
                                [],
                            )
                        ),
                        self._json(
                            conflict
                            .get(
                                "sources",
                                {}
                            )
                            .get(
                                "filename",
                                [],
                            )
                        ),
                        self._json(
                            conflict
                            .get(
                                "sources",
                                {}
                            )
                            .get(
                                "content",
                                [],
                            )
                        ),
                    ),
                )

            # ------------------------------------------------
            # Section
            # ------------------------------------------------

            if analysis.section:

                conn.execute(
                    """
                    INSERT INTO sections (
                        normalized_name,
                        display_name,
                        first_seen_path
                    )
                    VALUES (?, ?, ?)

                    ON CONFLICT(normalized_name)
                    DO UPDATE SET
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        analysis.section,
                        analysis.section,
                        str(path),
                    ),
                )

            # ------------------------------------------------
            # Project structure
            # ------------------------------------------------

            new_dirs = self._save_project_structure(
                conn,
                path,
            )

            # ------------------------------------------------
            # FTS
            # ------------------------------------------------

            fts_sync_document(
                conn,
                document_id,
            )

            conn.commit()

        # Папки считаем записанными только после успешного commit.
        self._structure_seen.update(
            new_dirs
        )

        # Известное состояние файла обновляем для инкрементальности.
        if self._known is not None:

            self._known[str(path)] = (
                scanned.size_bytes,
                scanned.mtime,
                str(extracted.status or ""),
                file_hash,
            )

        return document_id

    # ========================================================
    # Project structure
    # ========================================================

    def _save_project_structure(
        self,
        conn: sqlite3.Connection,
        path: Path,
    ) -> list[str]:
        """
        Записывает в project_structure цепочку папок файла.

        Папки, уже записанные в этом прогоне (_structure_seen),
        пропускаются. Возвращает список НОВЫХ записанных папок —
        вызывающий добавит их в кеш после commit.
        """

        root = self.project_path

        try:
            relative = path.relative_to(
                root
            )
        except ValueError:
            relative = path

        current = root

        depth = 0

        new_dirs: list[str] = []

        # Сам корень.
        key = str(current)

        if key not in self._structure_seen:

            self._insert_structure_item(
                conn,
                current,
                root,
                depth,
                item_type="directory",
            )

            new_dirs.append(key)

        for part in relative.parts[:-1]:

            current = current / part
            depth += 1

            key = str(current)

            if key in self._structure_seen:
                continue

            self._insert_structure_item(
                conn,
                current,
                root,
                depth,
                item_type="directory",
            )

            new_dirs.append(key)

        return new_dirs

    @staticmethod
    def _insert_structure_item(
        conn: sqlite3.Connection,
        path: Path,
        root: Path,
        depth: int,
        item_type: str,
    ) -> None:

        try:

            relative = path.relative_to(
                root
            )

            relative_path = str(
                relative
            )

        except ValueError:

            relative_path = str(
                path
            )

        parent = str(
            path.parent
        )

        conn.execute(
            """
            INSERT INTO project_structure (
                parent_path,
                path,
                name,
                item_type,
                extension,
                depth
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(path)
            DO UPDATE SET
                parent_path = excluded.parent_path,
                name = excluded.name,
                item_type = excluded.item_type,
                extension = excluded.extension,
                depth = excluded.depth,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                parent,
                str(path),
                path.name,
                item_type,
                path.suffix.lower(),
                depth,
            ),
        )

    # ========================================================
    # Remove deleted files
    # ========================================================

    def _remove_deleted_files(
        self,
        seen_paths: set[str],
    ) -> int:

        removed = 0

        rules = ExcludeRules(
            self.project_path,
            self.exclude,
        )

        with connect_db(
            self.project_path
        ) as conn:

            rows = conn.execute(
                """
                SELECT id, full_path
                FROM documents
                """
            ).fetchall()

            for row in rows:

                full_path = str(
                    row["full_path"]
                )

                if full_path in seen_paths:
                    continue

                # Убираем из индекса (исходные файлы не трогаем):
                #   * файл исчез из проекта;
                #   * файл попал под «Исключения из индекса».
                if (
                    not Path(
                        full_path
                    ).exists()
                    or (
                        rules
                        and rules.covers(
                            self.project_path,
                            full_path,
                        )
                    )
                ):

                    # Сначала убираем запись из FTS (нужны значения
                    # из documents/document_text), потом сам документ.
                    fts_delete_document(
                        conn,
                        int(row["id"]),
                    )

                    conn.execute(
                        """
                        DELETE FROM documents
                        WHERE id = ?
                        """,
                        (
                            row["id"],
                        ),
                    )

                    removed += 1

            conn.commit()

        if removed:
            logger.info(
                "Удалено из индекса (файлов больше нет): %d",
                removed,
            )

        return removed

    # ========================================================
    # Index one file
    # ========================================================

    def _index_one(
        self,
        scanned: ScannedFile,
    ) -> bool:

        if self.stop_event.is_set():
            return False

        # Текущий файл — только в памяти; в SQLite он попадёт
        # при ближайшем сбросе прогресса (_flush_progress).
        self.status[
            "current_file"
        ] = str(scanned.path)

        # ----------------------------------------------------
        # Incremental skip
        # ----------------------------------------------------

        if self._is_unchanged(
            scanned
        ):

            self.status[
                "skipped"
            ] += 1

            return False

        # ----------------------------------------------------
        # Extract
        # ----------------------------------------------------

        extracted = extract_document(
            scanned.path
        )

        if extracted.status == "error":

            self.status[
                "errors"
            ] += 1

            self.status[
                "error_message"
            ] = (
                f"{scanned.path}: "
                f"{extracted.error}"
            )

            logger.warning(
                "Ошибка чтения файла %s: %s",
                scanned.path,
                extracted.error,
            )

            # Даже при ошибке сохраняем
            # информацию о файле.
            try:

                self._save_document(
                    scanned,
                    extracted,
                )

            except Exception:

                logger.exception(
                    "Не удалось сохранить запись об ошибочном файле %s",
                    scanned.path,
                )

            return False

        # ----------------------------------------------------
        # Save
        # ----------------------------------------------------

        try:

            self._save_document(
                scanned,
                extracted,
            )

            self.status[
                "indexed"
            ] += 1

            return True

        except Exception as exc:

            self.status[
                "errors"
            ] += 1

            self.status[
                "error_message"
            ] = (
                f"{scanned.path}: {exc}"
            )

            logger.exception(
                "Ошибка сохранения документа %s",
                scanned.path,
            )

            return False

    # ========================================================
    # Progress (батчинг записи в SQLite)
    # ========================================================

    def _flush_progress(
        self,
        force: bool = False,
    ) -> None:
        """
        Сбрасывает прогресс в index_status не на каждый файл,
        а раз в INDEX_BATCH_SIZE файлов или раз в
        INDEX_PROGRESS_INTERVAL секунд (что наступит раньше).
        """

        self._since_flush += 1

        now = time.monotonic()

        if not force and not (
            self._since_flush >= INDEX_BATCH_SIZE
            or now - self._last_flush
            >= INDEX_PROGRESS_INTERVAL
        ):
            return

        self._since_flush = 0

        self._last_flush = now

        status = self.snapshot()

        try:

            update_index_status(
                self.project_path,
                scanned=status["scanned"],
                indexed=status["indexed"],
                skipped=status["skipped"],
                errors=status["errors"],
                total_files=status["total_files"],
                current_file=status["current_file"],
                error_message=status["error_message"],
            )

        except Exception:

            # Прогресс — не критичные данные: сбой записи
            # (например, долгая блокировка БД) не должен
            # прерывать индексацию.
            logger.warning(
                "Не удалось записать прогресс индексации",
                exc_info=True,
            )

    # ========================================================
    # Full index
    # ========================================================

    def run(self) -> dict[str, Any]:

        with self._lock:

            self.reset_stop()

            self.status = {
                "status": "indexing",
                "scanned": 0,
                "indexed": 0,
                "skipped": 0,
                "removed": 0,
                "errors": 0,
                "total_files": 0,
                "current_file": None,
                "error_message": None,
            }

            update_index_status(
                self.project_path,
                status="indexing",
                started_at=self._now(),
                finished_at=None,
                scanned=0,
                indexed=0,
                skipped=0,
                removed=0,
                errors=0,
                total_files=0,
                current_file=None,
                error_message=None,
            )

            seen_paths: set[str] = set()

            self._structure_seen = set()

            self._since_flush = 0

            self._last_flush = time.monotonic()

            started = time.monotonic()

            logger.info(
                "Индексация начата: %s",
                self.project_path,
            )

            try:

                # Состояние БД одним запросом — для
                # инкрементального пропуска без запроса на файл.
                self._known = self._load_known()

                # ------------------------------------------------
                # Scan
                # ------------------------------------------------

                for scanned in scan_project(
                    self.project_path,
                    self.exclude,
                ):

                    if self.stop_event.is_set():
                        break

                    self.status[
                        "scanned"
                    ] += 1

                    self.status[
                        "total_files"
                    ] = self.status[
                        "scanned"
                    ]

                    seen_paths.add(
                        str(
                            scanned.path
                        )
                    )

                    self._index_one(
                        scanned
                    )

                    # Прогресс в SQLite — батчами.
                    self._flush_progress()

                # ------------------------------------------------
                # Deleted
                # ------------------------------------------------

                if not self.stop_event.is_set():

                    removed = (
                        self._remove_deleted_files(
                            seen_paths
                        )
                    )

                    self.status[
                        "removed"
                    ] = removed

                # ------------------------------------------------
                # Finished
                # ------------------------------------------------

                if self.stop_event.is_set():

                    self.status[
                        "status"
                    ] = "stopped"

                else:

                    self.status[
                        "status"
                    ] = "idle"

                self.status[
                    "current_file"
                ] = None

                logger.info(
                    "Индексация завершена (%s) за %.1f с: "
                    "просмотрено=%d, проиндексировано=%d, "
                    "пропущено=%d, удалено=%d, ошибок=%d",
                    self.status["status"],
                    time.monotonic() - started,
                    self.status["scanned"],
                    self.status["indexed"],
                    self.status["skipped"],
                    self.status["removed"],
                    self.status["errors"],
                )

                update_index_status(
                    self.project_path,
                    status=self.status[
                        "status"
                    ],
                    finished_at=self._now(),
                    scanned=self.status[
                        "scanned"
                    ],
                    indexed=self.status[
                        "indexed"
                    ],
                    skipped=self.status[
                        "skipped"
                    ],
                    removed=self.status[
                        "removed"
                    ],
                    errors=self.status[
                        "errors"
                    ],
                    total_files=self.status[
                        "total_files"
                    ],
                    current_file=None,
                    error_message=self.status[
                        "error_message"
                    ],
                )

            except Exception as exc:

                logger.exception(
                    "Индексация прервана ошибкой: %s",
                    self.project_path,
                )

                self.status[
                    "status"
                ] = "error"

                self.status[
                    "errors"
                ] += 1

                self.status[
                    "error_message"
                ] = str(exc)

                self.status[
                    "current_file"
                ] = None

                update_index_status(
                    self.project_path,
                    status="error",
                    finished_at=self._now(),
                    scanned=self.status[
                        "scanned"
                    ],
                    indexed=self.status[
                        "indexed"
                    ],
                    skipped=self.status[
                        "skipped"
                    ],
                    removed=self.status[
                        "removed"
                    ],
                    errors=self.status[
                        "errors"
                    ],
                    total_files=self.status[
                        "total_files"
                    ],
                    current_file=None,
                    error_message=str(exc),
                )

            # Освобождаем кеши прогона.
            self._known = None

            self._structure_seen = set()

            return dict(
                self.status
            )


# ============================================================
# Convenience function
# ============================================================

def index_project(
    project_path: str | Path,
) -> dict[str, Any]:

    indexer = ProjectIndexer(
        project_path
    )

    return indexer.run()