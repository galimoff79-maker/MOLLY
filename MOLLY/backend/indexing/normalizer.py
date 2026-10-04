from __future__ import annotations

import re
from pathlib import Path


# ============================================================
# Molly PTO — Normalizer
# ============================================================


# ------------------------------------------------------------
# Общая нормализация текста
# ------------------------------------------------------------

def normalize_text(value: str | None) -> str:
    """
    Нормализует текст для поиска и анализа.

    Пример:
        "027 КЖ"
        "027-КЖ"
        "027_КЖ"
        "027КЖ"

    приводятся к сопоставимому виду.
    """

    if not value:
        return ""

    value = str(value)

    # Ё/ё → Е/е
    value = value.replace("Ё", "Е")
    value = value.replace("ё", "е")

    # Разные тире → обычный дефис
    value = value.replace("—", "-")
    value = value.replace("–", "-")
    value = value.replace("−", "-")

    # Неразрывный пробел
    value = value.replace("\xa0", " ")

    # Слэши и обратные слэши
    value = value.replace("\\", "/")

    # Убираем повторяющиеся пробелы
    value = re.sub(r"\s+", " ", value)

    return value.strip()


# ------------------------------------------------------------
# Нормализация поискового текста
# ------------------------------------------------------------

def normalize_for_search(value: str | None) -> str:
    """
    Более агрессивная нормализация для поиска.
    """

    value = normalize_text(value)

    value = value.lower()

    # Разделители считаем пробелами.
    value = re.sub(
        r"[_\-/]+",
        " ",
        value,
    )

    # Убираем лишнюю пунктуацию,
    # но сохраняем кириллицу, латиницу и цифры.
    value = re.sub(
        r"[^\w\s]",
        " ",
        value,
        flags=re.UNICODE,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


# ------------------------------------------------------------
# Нормализация обозначения раздела
# ------------------------------------------------------------

SECTION_RE = re.compile(
    r"""
    (?<!\d)
    (?P<number>\d{2,4})
    \s*
    [-_/]?
    \s*
    (?P<code>
        КЖ|КЖИ|КМ|КМД|КР|АР|АИ|ЭОМ|ЭО|ЭМ|ОВ|ВК|
        НВК|ТС|ТХ|ТМ|СС|ПС|АС|ГП|ПОС|ПОД|ПОР|ПБ|
        АТХ|АТД|НСС|ЛК|ТК|ИОС|ТБ|ПЗ|ПЗУ|ООС|ПБ|ДП
    )
    (?![\wА-Яа-я])
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize_section(value: str | None) -> str | None:
    """
    Ищет обозначение раздела.

    Примеры:

        027КЖ
        027-КЖ
        027 КЖ
        027_КЖ

    -> 027-КЖ
    """

    if not value:
        return None

    value = normalize_text(value)

    match = SECTION_RE.search(value)

    if not match:
        return None

    number = match.group("number")
    code = match.group("code").upper()

    # Нормализуем некоторые обозначения.
    aliases = {
        "КЖИ": "КЖ",
    }

    code = aliases.get(
        code,
        code,
    )

    return f"{number}-{code}"


def find_all_sections(value: str | None) -> list[str]:
    """
    Возвращает все найденные разделы без дублей.
    """

    if not value:
        return []

    value = normalize_text(value)

    result: list[str] = []

    for match in SECTION_RE.finditer(value):
        number = match.group("number")
        code = match.group("code").upper()

        if code == "КЖИ":
            code = "КЖ"

        section = f"{number}-{code}"

        if section not in result:
            result.append(section)

    return result


# ------------------------------------------------------------
# Нормализация номера документа
# ------------------------------------------------------------

def normalize_document_number(
    value: str | None,
) -> str | None:

    if not value:
        return None

    value = normalize_text(value)

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


# ------------------------------------------------------------
# Нормализация имени файла
# ------------------------------------------------------------

def normalize_filename(
    path: str | Path,
) -> str:

    return normalize_text(
        Path(path).name
    )


# ------------------------------------------------------------
# Нормализация пути
# ------------------------------------------------------------

def normalize_path(
    path: str | Path,
) -> str:

    return normalize_text(
        str(Path(path))
    )


# ------------------------------------------------------------
# Токены
# ------------------------------------------------------------

def tokenize(value: str | None) -> list[str]:
    """
    Разбивает текст на поисковые токены.
    """

    value = normalize_for_search(value)

    if not value:
        return []

    tokens = value.split()

    # Убираем слишком короткий мусор,
    # но оставляем обозначения вроде "КЖ".
    result: list[str] = []

    for token in tokens:
        if len(token) >= 2:
            result.append(token)

    return list(
        dict.fromkeys(result)
    )


# ------------------------------------------------------------
# Варианты написания раздела
# ------------------------------------------------------------

def section_variants(
    section: str | None,
) -> list[str]:

    if not section:
        return []

    section = normalize_section(section)

    if not section:
        return []

    number, code = section.split(
        "-",
        1,
    )

    variants = [
        f"{number}-{code}",
        f"{number} {code}",
        f"{number}_{code}",
        f"{number}{code}",
    ]

    return list(
        dict.fromkeys(variants)
    )


# ------------------------------------------------------------
# Сравнение разделов
# ------------------------------------------------------------

def sections_match(
    first: str | None,
    second: str | None,
) -> bool:

    first_normalized = normalize_section(first)
    second_normalized = normalize_section(second)

    if not first_normalized:
        return False

    if not second_normalized:
        return False

    return first_normalized == second_normalized


# ------------------------------------------------------------
# Нормализация названия документа
# ------------------------------------------------------------

def normalized_stem(
    path: str | Path,
) -> str:

    stem = Path(path).stem

    stem = normalize_for_search(
        stem
    )

    return stem


# ------------------------------------------------------------
# Безопасное имя для отображения
# ------------------------------------------------------------

def display_name(
    path: str | Path,
) -> str:

    return Path(path).name


# ------------------------------------------------------------
# Очистка текста документа
# ------------------------------------------------------------

def clean_document_text(
    value: str | None,
) -> str:

    if not value:
        return ""

    value = value.replace(
        "\x00",
        " ",
    )

    value = value.replace(
        "\r\n",
        "\n",
    )

    value = value.replace(
        "\r",
        "\n",
    )

    # Не уничтожаем переносы строк:
    # они нужны для анализа таблиц,
    # заголовков и реквизитов.
    value = re.sub(
        r"[ \t]+",
        " ",
        value,
    )

    value = re.sub(
        r"\n{4,}",
        "\n\n\n",
        value,
    )

    return value.strip()