from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from config import (
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
)
from database import connect_db
from logging_setup import get_logger
from indexing.normalizer import (
    normalize_for_search,
    normalize_section,
    tokenize,
)


# ============================================================
# Molly PTO — Search
# ============================================================


logger = get_logger("search")

# Сколько документов просматривает запасной LIKE-поиск
# (раньше было жёстко 5000 — на больших проектах часть
# документов в выдаче просто не участвовала).
LIKE_SCAN_LIMIT = 100_000

# Сколько кандидатов брать из FTS, если задан фильтр по разделу/типу
# (фильтры применяются в Python после выборки).
FILTERED_MIN_CANDIDATES = 500

# Тексты документов подгружаются пачками, чтобы не держать
# в памяти тексты всех кандидатов сразу.
TEXT_CHUNK = 50


def _fetch_texts(
    conn: Any,
    ids: list[int],
) -> dict[int, str]:
    """
    Тексты документов одним запросом
    (вместо отдельного SELECT на каждый документ).
    """

    if not ids:
        return {}

    placeholders = ",".join("?" * len(ids))

    rows = conn.execute(
        f"""
        SELECT document_id, text_content
        FROM document_text
        WHERE document_id IN ({placeholders})
        """,
        ids,
    ).fetchall()

    return {
        int(row["document_id"]): row["text_content"] or ""
        for row in rows
    }


@dataclass
class SearchResult:
    document_id: int
    filename: str
    full_path: str

    section: str | None
    document_type: str | None
    category: str | None

    document_number: str | None
    document_date: str | None

    work: str | None
    construction: str | None
    material: str | None

    parser_status: str | None
    classification_confidence: float

    score: float

    matched_fields: list[str]

    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ============================================================
# Helpers
# ============================================================


def _safe_limit(
    limit: int | None,
) -> int:

    if limit is None:
        return SEARCH_DEFAULT_LIMIT

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return SEARCH_DEFAULT_LIMIT

    return max(
        1,
        min(
            limit,
            SEARCH_MAX_LIMIT,
        ),
    )


def _normalize_query(
    query: str,
) -> str:

    return normalize_for_search(
        query or ""
    ).strip()


def _tokens(
    query: str,
) -> list[str]:

    normalized = _normalize_query(
        query
    )

    if not normalized:
        return []

    return tokenize(
        normalized
    )


def _build_fts_query(
    query: str,
) -> str:

    tokens = _tokens(query)

    if not tokens:
        return ""

    # FTS5 prefix search.
    #
    # Например:
    # 027-КЖ бетон плита
    #
    # превращается примерно в:
    # "027-КЖ"* AND "бетон"* AND "плита"*
    parts = []

    for token in tokens:

        token = token.strip()

        if not token:
            continue

        # FTS special characters.
        token = re.sub(
            r'["\':*()]',
            " ",
            token,
        ).strip()

        if not token:
            continue

        parts.append(
            f'"{token}"*'
        )

    return " AND ".join(
        parts
    )


def _make_snippet(
    text: str,
    query: str,
    radius: int = 220,
) -> str:

    if not text:
        return ""

    normalized_query = _normalize_query(
        query
    )

    if not normalized_query:
        return text[:radius * 2]

    lower_text = text.lower()

    # Сначала пытаемся найти всю фразу.
    position = lower_text.find(
        normalized_query.lower()
    )

    # Потом отдельные токены.
    if position < 0:

        for token in _tokens(query):

            position = lower_text.find(
                token.lower()
            )

            if position >= 0:
                break

    if position < 0:
        return text[:radius * 2]

    start = max(
        0,
        position - radius,
    )

    end = min(
        len(text),
        position + radius,
    )

    snippet = text[
        start:end
    ].strip()

    if start > 0:
        snippet = "…" + snippet

    if end < len(text):
        snippet += "…"

    return snippet


# ============================================================
# Field scoring
# ============================================================


def _field_score(
    query: str,
    row: Any,
) -> tuple[float, list[str]]:

    normalized_query = _normalize_query(
        query
    )

    tokens = _tokens(query)

    if not normalized_query:
        return 0.0, []

    fields = {
        "filename": row["filename"] or "",
        "path": row["full_path"] or "",
        "section": row["section"] or "",
        "document_type": row[
            "document_type"
        ]
        or "",
        "category": row["category"] or "",
        "document_number": row[
            "document_number"
        ]
        or "",
        "work": row["work"] or "",
        "construction": row[
            "construction"
        ]
        or "",
        "material": row["material"] or "",
    }

    weights = {
        "filename": 10.0,
        "section": 9.0,
        "document_number": 8.0,
        "document_type": 6.0,
        "work": 5.0,
        "construction": 5.0,
        "material": 4.0,
        "category": 3.0,
        "path": 2.0,
    }

    score = 0.0
    matched = []

    for name, value in fields.items():

        value_normalized = normalize_for_search(
            value
        )

        if not value_normalized:
            continue

        if (
            normalized_query
            in value_normalized
        ):

            score += weights[name]

            matched.append(
                name
            )

            continue

        field_hits = 0

        for token in tokens:

            if token.lower() in value_normalized:
                field_hits += 1

        if field_hits:

            score += (
                weights[name]
                * (
                    field_hits
                    / max(
                        len(tokens),
                        1,
                    )
                )
            )

            matched.append(
                name
            )

    return score, matched


# ============================================================
# Exact section filter
# ============================================================


def _section_matches(
    requested: str,
    actual: str | None,
) -> bool:

    if not requested:
        return True

    if not actual:
        return False

    normalized_requested = (
        normalize_section(
            requested
        )
    )

    normalized_actual = (
        normalize_section(
            actual
        )
    )

    if not normalized_requested:
        return True

    return (
        normalized_requested
        == normalized_actual
    )


# ============================================================
# Search using FTS
# ============================================================


def _search_fts(
    conn: Any,
    query: str,
    limit: int,
) -> list[SearchResult]:

    fts_query = _build_fts_query(
        query
    )

    if not fts_query:
        return []

    rows = conn.execute(
        """
        SELECT
            d.*,
            bm25(
                documents_fts,
                8.0,
                6.0,
                7.0,
                4.0,
                5.0,
                3.0,
                3.0,
                2.0,
                2.0,
                2.0,
                1.0
            ) AS fts_score

        FROM documents_fts

        JOIN documents d
            ON d.id = documents_fts.rowid

        WHERE documents_fts MATCH ?

        ORDER BY fts_score

        LIMIT ?
        """,
        (
            fts_query,
            limit,
        ),
    ).fetchall()

    results = []

    ids = [int(row["id"]) for row in rows]

    texts: dict[int, str] = {}

    for index, row in enumerate(rows):

        if index % TEXT_CHUNK == 0:

            texts = _fetch_texts(
                conn,
                ids[index:index + TEXT_CHUNK],
            )

        field_score, matched = (
            _field_score(
                query,
                row,
            )
        )

        # BM25 в SQLite возвращает
        # меньшие значения для лучших
        # совпадений.
        #
        # Поэтому превращаем его
        # в положительную добавку.
        raw_fts = float(
            row["fts_score"]
            or 0.0
        )

        fts_bonus = max(
            0.0,
            -raw_fts,
        )

        score = (
            field_score
            + fts_bonus
        )

        text_content = texts.get(
            int(row["id"]),
            "",
        )

        snippet = _make_snippet(
            text_content,
            query,
        )

        results.append(
            SearchResult(
                document_id=int(
                    row["id"]
                ),
                filename=row[
                    "filename"
                ],
                full_path=row[
                    "full_path"
                ],
                section=row[
                    "section"
                ],
                document_type=row[
                    "document_type"
                ],
                category=row[
                    "category"
                ],
                document_number=row[
                    "document_number"
                ],
                document_date=row[
                    "document_date"
                ],
                work=row[
                    "work"
                ],
                construction=row[
                    "construction"
                ],
                material=row[
                    "material"
                ],
                parser_status=row[
                    "parser_status"
                ],
                classification_confidence=float(
                    row[
                        "classification_confidence"
                    ]
                    or 0.0
                ),
                score=round(
                    score,
                    4,
                ),
                matched_fields=matched,
                snippet=snippet,
            )
        )

    return results


# ============================================================
# Search fallback
# ============================================================


def _search_like(
    conn: Any,
    query: str,
    limit: int,
) -> list[SearchResult]:

    tokens = _tokens(query)

    if not tokens:
        return []

    # Не строим огромный SQL: документы оцениваются Python-кодом.
    # Курсор читается потоково (без fetchall), тексты подгружаются
    # только для документов, у которых совпали поля.
    cursor = conn.execute(
        """
        SELECT *
        FROM documents
        ORDER BY
            classification_confidence DESC,
            updated_at DESC
        LIMIT ?
        """,
        (LIKE_SCAN_LIMIT,),
    )

    candidates = []

    for row in cursor:

        field_score, matched = (
            _field_score(
                query,
                row,
            )
        )

        if field_score > 0:
            candidates.append(
                (row, field_score, matched)
            )

    results = []

    texts: dict[int, str] = {}

    for index, (row, field_score, matched) in enumerate(
        candidates
    ):

        if index % TEXT_CHUNK == 0:

            texts = _fetch_texts(
                conn,
                [
                    int(item[0]["id"])
                    for item in candidates[
                        index:index + TEXT_CHUNK
                    ]
                ],
            )

        text_content = texts.get(
            int(row["id"]),
            "",
        )

        normalized_text = (
            normalize_for_search(
                text_content
            )
        )

        text_hits = 0

        for token in tokens:

            if token in normalized_text:
                text_hits += 1

        if text_hits:

            field_score += (
                1.5
                * text_hits
                / max(
                    len(tokens),
                    1,
                )
            )

            if "text" not in matched:
                matched.append(
                    "text"
                )

        if field_score <= 0:
            continue

        results.append(
            SearchResult(
                document_id=int(
                    row["id"]
                ),
                filename=row[
                    "filename"
                ],
                full_path=row[
                    "full_path"
                ],
                section=row[
                    "section"
                ],
                document_type=row[
                    "document_type"
                ],
                category=row[
                    "category"
                ],
                document_number=row[
                    "document_number"
                ],
                document_date=row[
                    "document_date"
                ],
                work=row[
                    "work"
                ],
                construction=row[
                    "construction"
                ],
                material=row[
                    "material"
                ],
                parser_status=row[
                    "parser_status"
                ],
                classification_confidence=float(
                    row[
                        "classification_confidence"
                    ]
                    or 0.0
                ),
                score=round(
                    field_score,
                    4,
                ),
                matched_fields=matched,
                snippet=_make_snippet(
                    text_content,
                    query,
                ),
            )
        )

    results.sort(
        key=lambda item: item.score,
        reverse=True,
    )

    return results[
        :limit
    ]


# ============================================================
# Main search
# ============================================================


def search_documents(
    project_path: str | Path,
    query: str,
    limit: int | None = None,
    section: str | None = None,
    document_type: str | None = None,
) -> list[dict[str, Any]]:

    """
    Основной поиск документов.

    Примеры:

        search_documents(
            project,
            "027-КЖ"
        )

        search_documents(
            project,
            "бетон монолитная плита",
            section="027-КЖ"
        )

        search_documents(
            project,
            "АОСР",
            document_type="АОСР"
        )
    """

    project_path = Path(
        project_path
    ).resolve()

    query = (
        query or ""
    ).strip()

    if not query:
        return []

    limit = _safe_limit(
        limit
    )

    # Фильтры по разделу/типу применяются после выборки, поэтому
    # при их наличии берём больше кандидатов, иначе выдача может
    # оказаться пустой при существующих совпадениях.
    fetch_limit = limit * 3

    if section or document_type:

        fetch_limit = max(
            limit * 20,
            FILTERED_MIN_CANDIDATES,
        )

    with connect_db(
        project_path
    ) as conn:

        try:

            results = _search_fts(
                conn,
                query,
                fetch_limit,
            )

        except Exception as exc:

            # FTS не должен ломать весь поиск —
            # переходим на запасной LIKE-поиск.
            logger.warning(
                "FTS-поиск не удался для %r: %s",
                query,
                exc,
            )

            results = []

        if not results:

            results = _search_like(
                conn,
                query,
                fetch_limit,
            )

        filtered = []

        for result in results:

            if section:

                if not _section_matches(
                    section,
                    result.section,
                ):
                    continue

            if document_type:

                actual = (
                    result.document_type
                    or ""
                ).lower()

                requested = (
                    document_type
                    .strip()
                    .lower()
                )

                if requested not in actual:

                    continue

            filtered.append(
                result
            )

        # Убираем возможные дубликаты.
        unique = {}

        for result in filtered:

            old = unique.get(
                result.document_id
            )

            if (
                old is None
                or result.score
                > old.score
            ):
                unique[
                    result.document_id
                ] = result

        final_results = sorted(
            unique.values(),
            key=lambda item: item.score,
            reverse=True,
        )[:limit]

        return [
            result.to_dict()
            for result in final_results
        ]


# ============================================================
# Exact document lookup
# ============================================================


def get_document(
    project_path: str | Path,
    document_id: int,
) -> dict[str, Any] | None:

    project_path = Path(
        project_path
    ).resolve()

    with connect_db(
        project_path
    ) as conn:

        row = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
            """,
            (
                document_id,
            ),
        ).fetchone()

        if row is None:
            return None

        result = dict(row)

        text_row = conn.execute(
            """
            SELECT *
            FROM document_text
            WHERE document_id = ?
            """,
            (
                document_id,
            ),
        ).fetchone()

        result["text"] = (
            text_row["text_content"]
            if text_row
            else ""
        )

        result["page_count"] = (
            text_row["page_count"]
            if text_row
            else 0
        )

        parts = conn.execute(
            """
            SELECT *
            FROM document_parts
            WHERE document_id = ?
            ORDER BY
                part_number,
                id
            """,
            (
                document_id,
            ),
        ).fetchall()

        result["parts"] = [
            dict(part)
            for part in parts
        ]

        conflicts = conn.execute(
            """
            SELECT *
            FROM document_conflicts
            WHERE document_id = ?
            ORDER BY id
            """,
            (
                document_id,
            ),
        ).fetchall()

        result["conflicts"] = [
            dict(conflict)
            for conflict in conflicts
        ]

        return result