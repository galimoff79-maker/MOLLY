from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from indexing.normalizer import (
    find_all_sections,
    normalize_section,
    normalize_text,
)


# ============================================================
# Molly PTO — Document Analyzer
# ============================================================


# Сколько первых символов текста анализируется классификатором.
CLASSIFY_TEXT_LIMIT = 120_000


@dataclass
class Evidence:
    source: str
    value: str
    weight: float
    description: str


@dataclass
class AnalysisResult:
    section: str | None = None
    sections: list[str] = field(default_factory=list)

    object_name: str | None = None

    document_type: str | None = None
    category: str | None = None
    stage: str | None = None

    document_number: str | None = None
    document_date: str | None = None

    organization: str | None = None
    author: str | None = None
    performer: str | None = None

    work: str | None = None
    construction: str | None = None
    material: str | None = None

    executive: bool = False
    project_document: bool = False

    confidence: float = 0.0

    evidence: list[Evidence] = field(
        default_factory=list
    )

    conflicts: list[dict] = field(
        default_factory=list
    )


# ============================================================
# Document type rules
# ============================================================

DOCUMENT_TYPE_RULES: list[
    tuple[str, str, str, list[str]]
] = [
    (
        "АОСР",
        "АОСР",
        "executive",
        [
            r"\bАОСР\b",
            r"акт\s+освидетельствования\s+скрытых\s+работ",
        ],
    ),
    (
        "АОК",
        "АОК",
        "executive",
        [
            r"\bАОК\b",
            r"акт\s+освидетельствования\s+ответствен",
        ],
    ),
    (
        "КС-2",
        "КС-2",
        "cost",
        [
            r"\bКС[- ]?2\b",
            r"акт\s+о\s+приемке\s+выполненных\s+работ",
        ],
    ),
    (
        "КС-3",
        "КС-3",
        "cost",
        [
            r"\bКС[- ]?3\b",
            r"справка\s+о\s+стоимости\s+выполненных",
        ],
    ),
    (
        "КС-6а",
        "КС-6а",
        "cost",
        [
            r"\bКС[- ]?6\s*а\b",
            r"\bКС6А\b",
            r"журнал\s+уч[её]та\s+выполненных\s+работ",
        ],
    ),
    (
        "КС-6",
        "КС-6",
        "cost",
        [
            r"\bКС[- ]?6\b",
            r"общий\s+журнал\s+работ",
        ],
    ),
    (
        "ИСПОЛНИТЕЛЬНАЯ СХЕМА",
        "ИСПОЛНИТЕЛЬНАЯ СХЕМА",
        "executive",
        [
            r"исполнительн(?:ая|ой|ую)\s+схем",
            r"исполнительн(?:ая|ого|ый)\s+чертеж",
        ],
    ),
    (
        "ИСПОЛНИТЕЛЬНЫЙ ЧЕРТЕЖ",
        "ИСПОЛНИТЕЛЬНЫЙ ЧЕРТЕЖ",
        "executive",
        [
            r"исполнительн(?:ый|ого|ом)\s+чертеж",
        ],
    ),
    (
        "ПРОТОКОЛ",
        "ПРОТОКОЛ",
        "executive",
        [
            r"\bпротокол\b",
            r"протокол\s+испытан",
            r"протокол\s+лаборатор",
        ],
    ),
    (
        "ПАСПОРТ",
        "ПАСПОРТ",
        "executive",
        [
            r"\bпаспорт\b",
            r"паспорт\s+качества",
            r"паспорт\s+изделия",
        ],
    ),
    (
        "СЕРТИФИКАТ",
        "СЕРТИФИКАТ",
        "executive",
        [
            r"\bсертификат\b",
            r"сертификат\s+соответств",
            r"сертификат\s+качества",
        ],
    ),
    (
        "ПИСЬМО",
        "ПИСЬМО",
        "correspondence",
        [
            r"\bписьмо\b",
            r"исходящ(?:ее|его)\s+письмо",
            r"входящ(?:ее|его)\s+письмо",
        ],
    ),
    (
        "ТЕХНИЧЕСКОЕ РЕШЕНИЕ",
        "ТЕХНИЧЕСКОЕ РЕШЕНИЕ",
        "correspondence",
        [
            r"техническ(?:ое|ого)\s+решени",
            r"\bтехрешени",
        ],
    ),
    (
        "РД",
        "РД",
        "project",
        [
            r"\bРД\b",
            r"рабоч(?:ая|ей)\s+документац",
            r"рабоч(?:ий|его)\s+чертеж",
        ],
    ),
    (
        "ПД",
        "ПД",
        "project",
        [
            r"\bПД\b",
            r"проектн(?:ая|ой)\s+документац",
        ],
    ),
    (
        "СПЕЦИФИКАЦИЯ",
        "СПЕЦИФИКАЦИЯ",
        "project",
        [
            r"\bспецификаци",
        ],
    ),
    (
        "ДОГОВОР",
        "ДОГОВОР",
        "general",
        [
            r"\bдоговор\b",
            r"\bконтракт\b",
        ],
    ),
    (
        "МСГ",
        "МСГ",
        "general",
        [
            r"\bМСГ\b",
            r"месячно[- ]суточн(?:ый|ого)\s+график",
        ],
    ),
]


# ============================================================
# Construction / material keywords
# ============================================================

MATERIAL_PATTERNS = [
    "бетон",
    "арматур",
    "цемент",
    "щебень",
    "песок",
    "раствор",
    "металл",
    "сталь",
    "труба",
    "изоляц",
    "гидроизоляц",
    "утеплител",
    "кирпич",
    "блок",
    "сварочн",
    "электрод",
    "кабель",
    "провод",
    "грунт",
    "асфальт",
]


CONSTRUCTION_PATTERNS = [
    "фундамент",
    "плита",
    "стен",
    "колонн",
    "балк",
    "перекрыт",
    "ростверк",
    "свай",
    "лестниц",
    "кровл",
    "покрыт",
    "монолит",
    "основан",
    "трубопровод",
    "эстакад",
    "резервуар",
    "здани",
    "сооружен",
]


WORK_PATTERNS = [
    "бетонирован",
    "бетонировани",
    "армирован",
    "монтаж",
    "демонтаж",
    "сварк",
    "укладк",
    "устройств",
    "разработк",
    "засыпк",
    "обратн",
    "изоляц",
    "гидроизоляц",
    "установк",
    "прокладк",
    "испытан",
    "контрол",
]


# ============================================================
# Date patterns
# ============================================================

DATE_PATTERNS = [
    re.compile(
        r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b"
    ),
    re.compile(
        r"\b(\d{1,2}\s+"
        r"(?:января|февраля|марта|апреля|мая|июня|"
        r"июля|августа|сентября|октября|ноября|декабря)"
        r"\s+\d{4})\b",
        re.IGNORECASE,
    ),
]


# ============================================================
# Document number patterns
# ============================================================

DOCUMENT_NUMBER_PATTERNS = [
    re.compile(
        r"(?:№|N|No\.?)\s*"
        r"([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9./_-]{1,50})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:номер|№)\s*"
        r"([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9./_-]{1,50})",
        re.IGNORECASE,
    ),
]


# ============================================================
# Helpers
# ============================================================

def _first_match(
    patterns: list[str],
    text: str,
) -> str | None:

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return match.group(0)

    return None


def _find_keyword(
    patterns: list[str],
    text: str,
) -> str | None:

    text_lower = text.lower()

    for pattern in patterns:

        match = re.search(
            pattern,
            text_lower,
            re.IGNORECASE,
        )

        if match:
            return match.group(0)

    return None


def _extract_date(
    text: str,
) -> str | None:

    for pattern in DATE_PATTERNS:

        match = pattern.search(text)

        if match:
            return match.group(1)

    return None


def _extract_document_number(
    text: str,
) -> str | None:

    for pattern in DOCUMENT_NUMBER_PATTERNS:

        match = pattern.search(text)

        if match:
            value = match.group(1).strip()

            # Защита от случайного захвата огромного текста.
            if 1 <= len(value) <= 60:
                return value

    return None


def _add_evidence(
    result: AnalysisResult,
    source: str,
    value: str,
    weight: float,
    description: str,
) -> None:

    result.evidence.append(
        Evidence(
            source=source,
            value=value,
            weight=weight,
            description=description,
        )
    )


# ============================================================
# Section analysis
# ============================================================

def analyze_sections(
    path: Path,
    text: str,
    result: AnalysisResult,
) -> None:

    filename = path.name
    parent_path = str(path.parent)

    # --------------------------------------------------------
    # Filename — сильный источник
    # --------------------------------------------------------

    filename_sections = find_all_sections(
        filename
    )

    for section in filename_sections:

        _add_evidence(
            result,
            "filename",
            section,
            0.95,
            "Раздел найден в имени файла.",
        )

    # --------------------------------------------------------
    # Path — тоже сильный источник
    # --------------------------------------------------------

    path_sections = find_all_sections(
        parent_path
    )

    for section in path_sections:

        _add_evidence(
            result,
            "path",
            section,
            0.90,
            "Раздел найден в пути к файлу.",
        )

    # --------------------------------------------------------
    # Content — очень сильный источник
    # --------------------------------------------------------

    # Не анализируем бесконечный текст целиком.
    # Для классификации достаточно первых 120000 символов.
    content_sample = text[:CLASSIFY_TEXT_LIMIT]

    content_sections = find_all_sections(
        content_sample
    )

    for section in content_sections:

        _add_evidence(
            result,
            "content",
            section,
            0.98,
            "Раздел найден в содержимом документа.",
        )

    # --------------------------------------------------------
    # Выбор основного раздела
    # --------------------------------------------------------

    scores: dict[str, float] = {}

    for evidence in result.evidence:

        if evidence.value in (
            filename_sections
            + path_sections
            + content_sections
        ):

            scores[evidence.value] = (
                scores.get(
                    evidence.value,
                    0.0,
                )
                + evidence.weight
            )

    if scores:

        ordered = sorted(
            scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )

        result.section = ordered[0][0]

        result.sections = [
            section
            for section, _score
            in ordered
        ]


# ============================================================
# Document type analysis
# ============================================================

def analyze_document_type(
    path: Path,
    text: str,
    result: AnalysisResult,
) -> None:

    filename = path.name

    # Сначала имя файла и путь.
    name_source = (
        f"{filename}\n"
        f"{path.parent}"
    )

    content_sample = text[:CLASSIFY_TEXT_LIMIT]

    candidates: list[
        tuple[str, str, str, float, str]
    ] = []

    for code, name, category, patterns in DOCUMENT_TYPE_RULES:

        name_match = False
        content_match = False

        for pattern in patterns:

            if re.search(
                pattern,
                name_source,
                re.IGNORECASE,
            ):
                name_match = True

            if re.search(
                pattern,
                content_sample,
                re.IGNORECASE,
            ):
                content_match = True

        if name_match:

            candidates.append(
                (
                    code,
                    name,
                    category,
                    0.95,
                    "filename/path",
                )
            )

        if content_match:

            candidates.append(
                (
                    code,
                    name,
                    category,
                    0.98,
                    "content",
                )
            )

    if not candidates:
        return

    scores: dict[str, float] = {}

    details: dict[
        str,
        tuple[str, str, float, str],
    ] = {}

    for code, name, category, weight, source in candidates:

        scores[code] = (
            scores.get(
                code,
                0.0,
            )
            + weight
        )

        details[code] = (
            name,
            category,
            weight,
            source,
        )

        _add_evidence(
            result,
            source,
            code,
            weight,
            f"Определён тип документа: {name}.",
        )

    best_code = max(
        scores,
        key=scores.get,
    )

    name, category, _weight, _source = details[
        best_code
    ]

    result.document_type = best_code
    result.category = category

    if category == "executive":
        result.executive = True

    if category == "project":
        result.project_document = True

    # --------------------------------------------------------
    # Стадия
    # --------------------------------------------------------

    if category == "project":
        result.stage = "project"

    elif category == "executive":
        result.stage = "executive"

    elif category == "cost":
        result.stage = "cost"

    elif category == "correspondence":
        result.stage = "correspondence"

    else:
        result.stage = "general"


# ============================================================
# Metadata analysis
# ============================================================

def analyze_metadata(
    path: Path,
    text: str,
    result: AnalysisResult,
) -> None:

    content_sample = text[:CLASSIFY_TEXT_LIMIT]

    # --------------------------------------------------------
    # Номер
    # --------------------------------------------------------

    number = _extract_document_number(
        content_sample
    )

    if number:

        result.document_number = number

        _add_evidence(
            result,
            "content",
            number,
            0.90,
            "Найден номер документа.",
        )

    # --------------------------------------------------------
    # Дата
    # --------------------------------------------------------

    date = _extract_date(
        content_sample
    )

    if date:

        result.document_date = date

        _add_evidence(
            result,
            "content",
            date,
            0.90,
            "Найдена дата документа.",
        )

    # --------------------------------------------------------
    # Материал
    # --------------------------------------------------------

    material = _find_keyword(
        MATERIAL_PATTERNS,
        content_sample,
    )

    if material:

        result.material = material

        _add_evidence(
            result,
            "content",
            material,
            0.70,
            "Найдено упоминание материала.",
        )

    # --------------------------------------------------------
    # Конструкция
    # --------------------------------------------------------

    construction = _find_keyword(
        CONSTRUCTION_PATTERNS,
        content_sample,
    )

    if construction:

        result.construction = construction

        _add_evidence(
            result,
            "content",
            construction,
            0.70,
            "Найдено упоминание конструкции.",
        )

    # --------------------------------------------------------
    # Работа
    # --------------------------------------------------------

    work = _find_keyword(
        WORK_PATTERNS,
        content_sample,
    )

    if work:

        result.work = work

        _add_evidence(
            result,
            "content",
            work,
            0.70,
            "Найдено упоминание вида работ.",
        )


# ============================================================
# Object detection
# ============================================================

OBJECT_PATTERNS = [
    r"объект[:\s]+([^\n]{5,200})",
    r"наименование\s+объекта[:\s]+([^\n]{5,200})",
    r"наименование\s+объекта\s+строительства[:\s]+([^\n]{5,200})",
]


def analyze_object(
    text: str,
    result: AnalysisResult,
) -> None:

    content_sample = text[:CLASSIFY_TEXT_LIMIT]

    for pattern in OBJECT_PATTERNS:

        match = re.search(
            pattern,
            content_sample,
            re.IGNORECASE,
        )

        if not match:
            continue

        value = normalize_text(
            match.group(1)
        )

        if not value:
            continue

        value = value[:250]

        result.object_name = value

        _add_evidence(
            result,
            "content",
            value,
            0.85,
            "Найдено наименование объекта.",
        )

        return


# ============================================================
# Organization detection
# ============================================================

ORGANIZATION_PATTERNS = [
    r"организаци[яи]\s*[:\-]\s*([^\n]{3,200})",
    r"подрядчик\s*[:\-]\s*([^\n]{3,200})",
    r"генподрядчик\s*[:\-]\s*([^\n]{3,200})",
    r"заказчик\s*[:\-]\s*([^\n]{3,200})",
    r"исполнитель\s*[:\-]\s*([^\n]{3,200})",
]


def analyze_organization(
    text: str,
    result: AnalysisResult,
) -> None:

    content_sample = text[:CLASSIFY_TEXT_LIMIT]

    for pattern in ORGANIZATION_PATTERNS:

        match = re.search(
            pattern,
            content_sample,
            re.IGNORECASE,
        )

        if not match:
            continue

        value = normalize_text(
            match.group(1)
        )

        if not value:
            continue

        value = value[:250]

        if "заказчик" in pattern:
            result.organization = value

        elif "исполнитель" in pattern:
            result.performer = value

        else:
            result.organization = value

        _add_evidence(
            result,
            "content",
            value,
            0.75,
            "Найдена организация/участник документа.",
        )

        # Здесь намеренно не выходим:
        # в одном документе могут быть заказчик,
        # подрядчик и исполнитель.


# ============================================================
# Conflict detection
# ============================================================

def detect_section_conflicts(
    path: Path,
    text: str,
    result: AnalysisResult,
) -> None:

    filename_sections = set(
        find_all_sections(
            path.name
        )
    )

    path_sections = set(
        find_all_sections(
            str(path.parent)
        )
    )

    content_sections = set(
        find_all_sections(
            text[:CLASSIFY_TEXT_LIMIT]
        )
    )

    sources = {
        "filename": filename_sections,
        "path": path_sections,
        "content": content_sections,
    }

    non_empty = {
        source: values
        for source, values in sources.items()
        if values
    }

    if len(non_empty) < 2:
        return

    all_values = set()

    for values in non_empty.values():
        all_values.update(values)

    if len(all_values) <= 1:
        return

    result.conflicts.append(
        {
            "field": "section",
            "description": (
                "Разделы, найденные в имени, пути "
                "и содержимом документа, различаются."
            ),
            "sources": {
                source: sorted(values)
                for source, values in non_empty.items()
            },
        }
    )


# ============================================================
# Confidence
# ============================================================

def calculate_confidence(
    result: AnalysisResult,
) -> float:

    if not result.evidence:
        return 0.0

    # Самая сильная комбинация:
    # несколько независимых подтверждений.
    total = sum(
        evidence.weight
        for evidence in result.evidence
    )

    unique_sources = {
        evidence.source
        for evidence in result.evidence
    }

    # Базовый показатель.
    confidence = min(
        total / 3.0,
        1.0,
    )

    # Дополнительное подтверждение
    # независимыми источниками.
    if len(unique_sources) >= 2:
        confidence += 0.08

    if len(unique_sources) >= 3:
        confidence += 0.05

    # Конфликт снижает уверенность.
    if result.conflicts:
        confidence -= 0.20

    confidence = max(
        0.0,
        min(
            confidence,
            0.99,
        ),
    )

    return round(
        confidence,
        3,
    )


# ============================================================
# Main analysis function
# ============================================================

def analyze_document(
    path: str | Path,
    text: str = "",
) -> AnalysisResult:

    path = Path(path)

    result = AnalysisResult()

    # --------------------------------------------------------
    # Нормализуем вход
    # --------------------------------------------------------

    text = normalize_text(
        text
    )

    # --------------------------------------------------------
    # Раздел
    # --------------------------------------------------------

    analyze_sections(
        path,
        text,
        result,
    )

    # --------------------------------------------------------
    # Тип документа
    # --------------------------------------------------------

    analyze_document_type(
        path,
        text,
        result,
    )

    # --------------------------------------------------------
    # Метаданные
    # --------------------------------------------------------

    analyze_metadata(
        path,
        text,
        result,
    )

    # --------------------------------------------------------
    # Объект
    # --------------------------------------------------------

    analyze_object(
        text,
        result,
    )

    # --------------------------------------------------------
    # Организации
    # --------------------------------------------------------

    analyze_organization(
        text,
        result,
    )

    # --------------------------------------------------------
    # Конфликты
    # --------------------------------------------------------

    detect_section_conflicts(
        path,
        text,
        result,
    )

    # --------------------------------------------------------
    # Уверенность
    # --------------------------------------------------------

    result.confidence = calculate_confidence(
        result
    )

    return result