from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from threading import Lock
from typing import Any

from config import (
    DATA_DIR,
    DEFAULT_PROJECT_DIR,
    PROJECT_CONFIG_FILE,
    ensure_directories,
)
from database import (
    connect_db,
    initialize_database,
)
from indexing.scanner import (
    validate_project_path,
)
from logging_setup import get_logger

logger = get_logger("project")


# ============================================================
# Запрещённые папки проекта
# ============================================================


def _env_path(name: str) -> Path | None:

    value = os.environ.get(name)

    if not value:
        return None

    try:
        return Path(value).resolve()
    except OSError:
        return None


def _forbidden_trees() -> list[Path]:
    """
    Папки, которые нельзя выбирать как проект
    ни сами, ни любую вложенную в них.
    """

    trees: list[Path] = [DATA_DIR.resolve()]

    if sys.platform == "win32":

        for name in (
            "SystemRoot",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "ProgramW6432",
            "ProgramData",
        ):

            path = _env_path(name)

            if path is not None:
                trees.append(path)

    else:

        for name in (
            "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64",
            "/proc", "/sbin", "/sys", "/usr", "/var", "/run",
        ):
            trees.append(Path(name))

    return trees


def _forbidden_exact() -> list[Path]:
    """
    Папки, которые нельзя выбрать САМИ (но их подпапки — можно):
    корень диска, профиль пользователя, папка всех профилей.
    """

    exact: list[Path] = []

    home = Path.home().resolve()

    exact.append(home)
    exact.append(home.parent)

    for name in ("USERPROFILE", "PUBLIC", "ALLUSERSPROFILE"):

        path = _env_path(name)

        if path is not None:
            exact.append(path)

    return exact


def check_project_path_allowed(path: Path) -> None:
    """
    Бросает ValueError, если путь — корень диска или системная /
    служебная папка. Исходные файлы проекта Molly не меняет, но
    индексация всего C:\\Windows или корня диска бессмысленна
    и опасна по времени/нагрузке.
    """

    path = path.resolve()

    # Корень диска / "/".
    if path.parent == path:
        raise ValueError(
            "Нельзя выбрать корень диска как папку проекта: "
            f"{path}"
        )

    for tree in _forbidden_trees():

        if path == tree or tree in path.parents:

            raise ValueError(
                "Нельзя выбрать системную или служебную папку "
                f"как проект: {path}"
            )

    for exact in _forbidden_exact():

        if path == exact:

            raise ValueError(
                "Нельзя выбрать эту папку как проект "
                f"(слишком общая): {path}. "
                "Выберите папку конкретного проекта."
            )


# ============================================================
# Molly PTO — Project Manager
# ============================================================


class ProjectManager:
    """
    Управляет выбранным проектом Molly.

    Важно:

    - исходная папка проекта не изменяется;
    - Molly не переименовывает файлы;
    - Molly не перемещает файлы;
    - для каждого проекта используется собственная SQLite БД;
    - выбранный путь сохраняется в конфигурации Molly.
    """

    def __init__(self) -> None:

        ensure_directories()

        self._lock = Lock()

        self._project_path: Path | None = None

        self._load_saved_project()

    # ========================================================
    # Saved configuration
    # ========================================================

    def _load_saved_project(self) -> None:

        path = PROJECT_CONFIG_FILE

        if not path.exists():

            # Первый запуск: проект из MOLLY_PROJECT_DIR (если задан).
            if DEFAULT_PROJECT_DIR is None:
                return

            default = Path(
                DEFAULT_PROJECT_DIR
            )

            if default.exists():

                self._project_path = (
                    default.resolve()
                )

                self._save_config()

                logger.info(
                    "Проект по умолчанию (MOLLY_PROJECT_DIR): %s",
                    self._project_path,
                )

            else:

                logger.warning(
                    "MOLLY_PROJECT_DIR не существует: %s",
                    default,
                )

            return

        try:

            # utf-8-sig: переживает файл, пересохранённый
            # Блокнотом Windows с BOM.
            raw = path.read_text(
                encoding="utf-8-sig"
            )

            data = json.loads(
                raw
            )

            saved_path = (
                data.get(
                    "project_path"
                )
            )

            if not saved_path:
                return

            candidate = Path(
                saved_path
            ).resolve()

            if candidate.exists():

                self._project_path = (
                    candidate
                )

                logger.info(
                    "Загружен сохранённый проект: %s",
                    candidate,
                )

            else:

                logger.warning(
                    "Сохранённая папка проекта недоступна: %s",
                    candidate,
                )

        except Exception:
            # Повреждённая конфигурация
            # не должна ломать запуск Molly.
            logger.exception(
                "Не удалось прочитать %s",
                path,
            )

            self._project_path = None

    def _save_config(self) -> None:

        ensure_directories()

        data = {
            "project_path": (
                str(
                    self._project_path
                )
                if self._project_path
                else None
            )
        }

        # Атомарная запись: временный файл + replace,
        # чтобы сбой не оставил пустой/битый project.json.
        tmp = PROJECT_CONFIG_FILE.with_suffix(".json.tmp")

        tmp.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        os.replace(
            tmp,
            PROJECT_CONFIG_FILE,
        )

    # ========================================================
    # Get
    # ========================================================

    @property
    def project_path(self) -> Path | None:

        return self._project_path

    def get_project_path(self) -> str | None:

        if self._project_path is None:
            return None

        return str(
            self._project_path
        )

    # ========================================================
    # Project info
    # ========================================================

    def get_project_info(
        self,
    ) -> dict[str, Any]:

        path = self._project_path

        if path is None:

            return {
                "selected": False,
                "path": None,
                "name": None,
                "exists": False,
                "database": None,
            }

        try:

            db_path = initialize_database(
                path
            )

        except Exception:

            logger.exception(
                "Не удалось инициализировать БД проекта %s",
                path,
            )

            db_path = None

        info = {
            "selected": True,
            "path": str(path),
            "name": path.name,
            "exists": path.exists(),
            "is_directory": path.is_dir(),
            "database": (
                str(db_path)
                if db_path
                else None
            ),
        }

        # Информация из project_info.
        #
        # Таблица содержит ОДНУ строку (id = 1) с колонками
        # project_path, project_name, created_at, updated_at —
        # а не пары key/value. Ключи ответа не пересекаются
        # с "path"/"name", которые уже заполнены выше.
        if db_path is not None:

            try:

                with connect_db(
                    path
                ) as conn:

                    row = conn.execute(
                        """
                        SELECT
                            project_path,
                            project_name,
                            created_at,
                            updated_at
                        FROM project_info
                        WHERE id = 1
                        """
                    ).fetchone()

                if row is not None:

                    info["db_project_path"] = row["project_path"]
                    info["db_project_name"] = row["project_name"]
                    info["created_at"] = row["created_at"]
                    info["updated_at"] = row["updated_at"]

            except Exception:

                logger.exception(
                    "Не удалось прочитать project_info для %s",
                    path,
                )

        return info

    # ========================================================
    # Set project
    # ========================================================

    def set_project(
        self,
        project_path: str | Path,
    ) -> dict[str, Any]:

        with self._lock:

            candidate = Path(
                project_path
            ).expanduser().resolve()

            # Проверяем проект ДО изменения
            # сохранённого пути.
            check_project_path_allowed(
                candidate
            )

            # validate_project_path возвращает (ok, message),
            # а не бросает исключение — результат нужно проверять.
            ok, message = validate_project_path(
                candidate
            )

            if not ok:

                logger.warning(
                    "Проект отклонён: %s",
                    message,
                )

                raise ValueError(
                    message
                )

            self._project_path = candidate

            # Создаём только служебную БД
            # Molly.
            #
            # В папку самого проекта
            # ничего не записываем.
            initialize_database(
                candidate
            )

            self._save_config()

            logger.info(
                "Выбран проект: %s",
                candidate,
            )

            return self.get_project_info()

    # ========================================================
    # Clear project
    # ========================================================

    def clear_project(self) -> None:

        with self._lock:

            self._project_path = None

            self._save_config()

            logger.info(
                "Выбор проекта сброшен"
            )

    # ========================================================
    # Require project
    # ========================================================

    def require_project(self) -> Path:

        path = self._project_path

        if path is None:

            raise RuntimeError(
                "Проект не выбран."
            )

        if not path.exists():

            raise RuntimeError(
                "Выбранная папка проекта "
                "больше не существует: "
                f"{path}"
            )

        if not path.is_dir():

            raise RuntimeError(
                "Выбранный путь не является "
                f"папкой проекта: {path}"
            )

        return path


# ============================================================
# Global manager
# ============================================================


_manager: ProjectManager | None = None


def get_project_manager() -> ProjectManager:

    global _manager

    if _manager is None:

        _manager = ProjectManager()

    return _manager


def get_current_project() -> Path | None:

    return (
        get_project_manager()
        .project_path
    )


def require_current_project() -> Path:

    return (
        get_project_manager()
        .require_project()
    )


def set_current_project(
    project_path: str | Path,
) -> dict[str, Any]:

    return (
        get_project_manager()
        .set_project(
            project_path
        )
    )