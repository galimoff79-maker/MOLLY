from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from config import (
    IGNORED_DIRECTORIES,
    IGNORED_EXTENSIONS,
    IGNORED_FILENAMES,
    SUPPORTED_EXTENSIONS,
)


# ============================================================
# Наборы для проверок (строятся один раз при импорте,
# а не заново на каждый файл)
# ============================================================

_IGNORED_FILENAMES = frozenset(
    name.lower()
    for name in IGNORED_FILENAMES
)

_IGNORED_EXTENSIONS = frozenset(
    ext.lower()
    for ext in IGNORED_EXTENSIONS
)

_SUPPORTED_EXTENSIONS = frozenset(
    ext.lower()
    for ext in SUPPORTED_EXTENSIONS
)

_IGNORED_DIRECTORIES = frozenset(
    name.lower()
    for name in IGNORED_DIRECTORIES
)


# ============================================================
# Molly PTO — File Scanner
# ============================================================


@dataclass
class ScannedFile:
    """
    Информация о найденном файле.

    Здесь файл только обнаруживается.
    Никаких изменений исходного файла не выполняется.
    """

    path: Path

    relative_path: str

    filename: str

    extension: str

    size_bytes: int

    mtime: float


# ============================================================
# Проверки
# ============================================================

def is_ignored_file(path: Path) -> bool:
    """
    Определяет, нужно ли полностью пропустить файл.
    """

    filename_lower = path.name.lower()
    extension_lower = path.suffix.lower()

    if filename_lower in _IGNORED_FILENAMES:
        return True

    if extension_lower in _IGNORED_EXTENSIONS:
        return True

    # Временные файлы Office.
    if filename_lower.startswith("~$"):
        return True

    # Временные AutoCAD-файлы.
    if filename_lower.endswith(
        (
            ".dwl",
            ".dwl2",
        )
    ):
        return True

    return False


def is_supported_file(path: Path) -> bool:
    """
    Проверяет, поддерживается ли формат файла.
    """

    if is_ignored_file(path):
        return False

    return path.suffix.lower() in _SUPPORTED_EXTENSIONS


def get_file_stat(
    path: Path,
) -> tuple[int, float]:

    try:

        stat = path.stat()

        return (
            int(stat.st_size),
            float(stat.st_mtime),
        )

    except (
        FileNotFoundError,
        PermissionError,
        OSError,
    ):

        return (
            0,
            0.0,
        )


# ============================================================
# Scanner
# ============================================================

class ExcludeRules:
    """
    Правила исключения из индекса (настройка «Папки исключений»).

    Строка правила может быть:
      * путём к папке или файлу — абсолютным или относительно папки
        проекта («Архив», «Черновики\\старое», «D:\\Проект\\Лишнее»);
      * маской по имени файла/папки («*.bak», «~*», «Копия*»).
    Сравнение без учёта регистра, как в Windows.
    """

    def __init__(self, root: Path, rules: list[str] | tuple[str, ...]) -> None:

        self.paths: set[str] = set()
        self.patterns: list[str] = []

        for rule in rules or ():

            rule = (rule or "").strip().strip('"')

            if not rule:
                continue

            if any(ch in rule for ch in "*?["):
                self.patterns.append(rule.lower())
                continue

            candidate = Path(rule)

            if not candidate.is_absolute():
                candidate = root / candidate

            self.paths.add(os.path.normcase(os.path.normpath(str(candidate))))

    def __bool__(self) -> bool:
        return bool(self.paths or self.patterns)

    def covers(self, root: Path, full_path: str) -> bool:
        """
        True, если файл исключён сам или лежит в исключённой папке
        (любого уровня вложенности внутри рабочей папки).
        """

        try:
            relative = Path(full_path).relative_to(root)
        except ValueError:
            return self.matches(full_path, Path(full_path).name)

        current = root

        for part in relative.parts:

            current = current / part

            if self.matches(str(current), part):
                return True

        return False

    def matches(self, full_path: str, name: str) -> bool:

        if os.path.normcase(os.path.normpath(full_path)) in self.paths:
            return True

        lowered = name.lower()

        return any(
            fnmatch.fnmatch(lowered, pattern)
            for pattern in self.patterns
        )


def scan_project(
    project_path: str | Path,
    exclude: list[str] | tuple[str, ...] | None = None,
) -> Iterator[ScannedFile]:
    """
    Рекурсивно сканирует проект.

    Важно:
    - ничего не удаляет;
    - ничего не перемещает;
    - ничего не переименовывает;
    - ничего не изменяет.

    Возвращает только поддерживаемые документы.
    """

    root = Path(
        project_path
    ).resolve()

    if not root.exists():
        raise FileNotFoundError(
            f"Папка проекта не существует: {root}"
        )

    if not root.is_dir():
        raise NotADirectoryError(
            f"Путь проекта не является папкой: {root}"
        )

    rules = ExcludeRules(root, exclude or ())

    # os.walk обычно заметно экономнее,
    # чем создание огромного списка Path.rglob().
    for current_root, dirnames, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):

        current_path = Path(
            current_root
        )

        # ----------------------------------------------------
        # Удаляем из обхода только заведомо мусорные каталоги.
        # Саму файловую систему не изменяем.
        # ----------------------------------------------------

        filtered_dirs: list[str] = []

        for dirname in dirnames:

            dirname_lower = dirname.lower()

            if dirname_lower in _IGNORED_DIRECTORIES:
                continue

            if rules and rules.matches(
                os.path.join(current_root, dirname),
                dirname,
            ):
                continue

            # Служебные каталоги AutoCAD/Office.
            if dirname_lower.startswith(
                "~$"
            ):
                continue

            filtered_dirs.append(
                dirname
            )

        dirnames[:] = filtered_dirs

        # ----------------------------------------------------
        # Файлы
        # ----------------------------------------------------

        for filename in filenames:

            path = current_path / filename

            # is_supported_file уже включает is_ignored_file.
            if not is_supported_file(path):
                continue

            if rules and rules.matches(
                os.path.join(current_root, filename),
                filename,
            ):
                continue

            try:

                size_bytes, mtime = get_file_stat(
                    path
                )

                relative_path = str(
                    path.relative_to(root)
                )

                yield ScannedFile(
                    path=path,
                    relative_path=relative_path,
                    filename=path.name,
                    extension=path.suffix.lower(),
                    size_bytes=size_bytes,
                    mtime=mtime,
                )

            except (
                FileNotFoundError,
                PermissionError,
                OSError,
                ValueError,
            ):
                # Файл мог быть удалён/перемещён
                # во время сканирования.
                continue


# ============================================================
# Быстрый подсчёт
# ============================================================

def count_project_files(
    project_path: str | Path,
) -> int:

    count = 0

    for _file in scan_project(
        project_path
    ):
        count += 1

    return count


# ============================================================
# Проверка проекта
# ============================================================

def validate_project_path(
    project_path: str | Path,
) -> tuple[bool, str]:

    path = Path(
        project_path
    ).resolve()

    if not path.exists():

        return (
            False,
            f"Папка не существует: {path}",
        )

    if not path.is_dir():

        return (
            False,
            f"Путь не является папкой: {path}",
        )

    try:

        # Проверяем, что каталог реально читается.
        next(
            os.scandir(path)
        )

    except StopIteration:
        return (
            True,
            "Папка существует, но пока пустая.",
        )

    except (
        PermissionError,
        OSError,
    ) as exc:

        return (
            False,
            f"Нет доступа к папке: {exc}",
        )

    return (
        True,
        "Папка проекта доступна.",
    )