from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from config import DATA_DIR
from logging_setup import get_logger

logger = get_logger("settings")

# ============================================================
# Настройки МОЛЛИ
# ============================================================
#
# data/settings.json — основной файл (запись атомарная).
# data/backups/settings_<время>.json — резервные копии
# (хранятся последние BACKUP_KEEP). Если основной файл повреждён,
# настройки восстанавливаются из последней копии.

SETTINGS_FILE = DATA_DIR / "settings.json"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 10

STYLES = ("business", "friendly", "strict")
VERBOSITY = ("brief", "detailed")
THEMES = ("system", "light", "dark")

DEFAULTS: dict[str, Any] = {
    "setup_done": False,
    "assistant_name": "МОЛЛИ",
    "user_name": "",
    "style": "business",
    "verbosity": "brief",
    "system_prompt": "",
    "use_documents": True,
    "top_k": 5,
    "theme": "system",
    "model": "molly-pto",
    "ollama_url": "http://127.0.0.1:11434",
    "provider": "ollama",            # ollama | freellmapi
    "model_mode": "auto",            # auto | manual (для freellmapi)
    "online_model": "",              # выбранная вручную модель FreeLLMAPI (пусто = AUTO)
    "freellmapi": {
        "enabled": False,
        "url": "http://127.0.0.1:31415",
    },
    "temperature": 0.3,
    "num_ctx": 8192,
    "max_tokens": 1024,
    "keep_alive": "5m",
    "excluded": [],
    "auto_reindex_minutes": 10,
    "mail": {
        "email": "",
        "imap_host": "imap.yandex.ru",
        "imap_port": 993,
        "smtp_host": "smtp.yandex.ru",
        "smtp_port": 465,
        "confirm_send": True,
    },
    "lan": {
        "enabled": False,
        "port": 8787,
        "token": "",
    },
}

# Поля, которые не попадают в экспорт и не принимаются при импорте.
SECRET_PATHS = (("lan", "token"), ("lan", "enabled"))

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError("ожидалось true/false")


def _int(lo: int, hi: int):
    def f(v: Any) -> int:
        if isinstance(v, bool):
            raise ValueError("ожидалось число")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError("ожидалось целое число")
        if not lo <= n <= hi:
            raise ValueError(f"допустимо от {lo} до {hi}")
        return n
    return f


def _float(lo: float, hi: float):
    def f(v: Any) -> float:
        if isinstance(v, bool):
            raise ValueError("ожидалось число")
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise ValueError("ожидалось число")
        if not lo <= n <= hi:
            raise ValueError(f"допустимо от {lo} до {hi}")
        return n
    return f


def _str(max_len: int, allow_empty: bool = True):
    def f(v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("ожидалась строка")
        v = v.strip() if max_len < 1000 else v.replace("\r\n", "\n")
        if len(v) > max_len:
            raise ValueError(f"слишком длинное значение (макс. {max_len})")
        if not allow_empty and not v:
            raise ValueError("не может быть пустым")
        return v
    return f


def _choice(options: tuple[str, ...]):
    def f(v: Any) -> str:
        if v not in options:
            raise ValueError("допустимо: " + ", ".join(options))
        return v
    return f


def _url(v: Any) -> str:
    if not isinstance(v, str):
        raise ValueError("ожидалась строка")
    v = v.strip().rstrip("/")
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("адрес должен начинаться с http:// или https://")
    return v


def _keep_alive(v: Any) -> str:
    if not isinstance(v, str):
        raise ValueError("ожидалась строка, например 5m, 30s, 0")
    v = v.strip()
    import re
    if not re.fullmatch(r"-?\d+(s|m|h)?", v):
        raise ValueError("формат: 5m, 30s, 1h или 0 (выгружать сразу)")
    return v


def _str_list(max_items: int, max_len: int):
    def f(v: Any) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("ожидался список")
        out: list[str] = []
        for item in v:
            if not isinstance(item, str):
                raise ValueError("элементы списка должны быть строками")
            item = item.strip()
            if item and item not in out:
                out.append(item[:max_len])
        return out[:max_items]
    return f


VALIDATORS: dict[str, Any] = {
    "setup_done": _bool,
    "assistant_name": _str(40, allow_empty=False),
    "user_name": _str(60),
    "style": _choice(STYLES),
    "verbosity": _choice(VERBOSITY),
    "system_prompt": _str(20000),
    "use_documents": _bool,
    "top_k": _int(1, 15),
    "theme": _choice(THEMES),
    "model": _str(200),
    "ollama_url": _url,
    "provider": _choice(("ollama", "freellmapi")),
    "model_mode": _choice(("auto", "manual")),
    "online_model": _str(200),
    "temperature": _float(0.0, 2.0),
    "num_ctx": _int(512, 131072),
    "max_tokens": _int(16, 32768),
    "keep_alive": _keep_alive,
    "excluded": _str_list(200, 500),
    "auto_reindex_minutes": _int(0, 1440),
}

FREELLMAPI_VALIDATORS: dict[str, Any] = {
    "enabled": _bool,
    "url": _url,
}

MAIL_VALIDATORS: dict[str, Any] = {
    "email": _str(200),
    "imap_host": _str(200, allow_empty=False),
    "imap_port": _int(1, 65535),
    "smtp_host": _str(200, allow_empty=False),
    "smtp_port": _int(1, 65535),
    "confirm_send": _bool,
}

LAN_VALIDATORS: dict[str, Any] = {
    "enabled": _bool,
    "port": _int(1024, 65535),
    "token": _str(64),
}


def _merge_defaults(data: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULTS)
    for key, value in data.items():
        if key in ("mail", "lan", "freellmapi"):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k in result[key]:
                        result[key][k] = v
        elif key in result:
            result[key] = value
    return result


def _validate_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Возвращает проверенную копию patch или бросает ValueError."""

    clean: dict[str, Any] = {}
    errors: list[str] = []

    for key, value in patch.items():
        if key in ("mail", "lan", "freellmapi"):
            validators = {
                "mail": MAIL_VALIDATORS,
                "lan": LAN_VALIDATORS,
                "freellmapi": FREELLMAPI_VALIDATORS,
            }[key]
            if not isinstance(value, dict):
                errors.append(f"{key}: ожидался объект")
                continue
            sub: dict[str, Any] = {}
            for k, v in value.items():
                if k not in validators:
                    continue
                try:
                    sub[k] = validators[k](v)
                except ValueError as exc:
                    errors.append(f"{key}.{k}: {exc}")
            clean[key] = sub
        elif key in VALIDATORS:
            try:
                clean[key] = VALIDATORS[key](value)
            except ValueError as exc:
                errors.append(f"{key}: {exc}")

    if errors:
        raise ValueError("; ".join(errors))

    return clean


def _read_file(path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("корень настроек должен быть объектом")
    return data


def _load_from_disk() -> dict[str, Any]:

    candidates = [SETTINGS_FILE]

    if BACKUP_DIR.exists():
        candidates += sorted(
            BACKUP_DIR.glob("settings_*.json"),
            reverse=True,
        )

    for index, path in enumerate(candidates):

        if not path.exists():
            continue

        try:
            data = _read_file(path)
            try:
                clean = _validate_patch(data)
            except ValueError:
                # Берём только корректные поля.
                clean = {}
                for k, v in data.items():
                    try:
                        clean.update(_validate_patch({k: v}))
                    except ValueError:
                        pass
            if index > 0:
                logger.warning(
                    "Настройки восстановлены из резервной копии %s",
                    path.name,
                )
            return _merge_defaults(clean)

        except Exception:
            logger.exception(
                "Не удалось прочитать настройки из %s",
                path,
            )

    return copy.deepcopy(DEFAULTS)


def _write(data: dict[str, Any]) -> None:

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if SETTINGS_FILE.exists():
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(
                SETTINGS_FILE,
                BACKUP_DIR / f"settings_{stamp}.json",
            )
            old = sorted(BACKUP_DIR.glob("settings_*.json"))
            for path in old[:-BACKUP_KEEP]:
                path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Не удалось сделать резервную копию настроек", exc_info=True)

    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, SETTINGS_FILE)


def get() -> dict[str, Any]:
    """Текущие настройки (копия)."""

    global _cache

    with _lock:
        if _cache is None:
            _cache = _load_from_disk()
        return copy.deepcopy(_cache)


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """Применяет частичное обновление. ValueError — при неверных значениях."""

    global _cache

    clean = _validate_patch(patch)

    with _lock:
        current = get()

        for key, value in clean.items():
            if key in ("mail", "lan", "freellmapi"):
                current[key].update(value)
            else:
                current[key] = value

        _write(current)
        _cache = current

    return copy.deepcopy(current)


def reload() -> None:
    global _cache
    with _lock:
        _cache = None


def new_lan_token() -> str:
    """Токен вида XXXX-XXXX без похожих символов (0/O, 1/I)."""

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def public_view(settings: dict[str, Any]) -> dict[str, Any]:
    """Копия без секретов — для экспорта."""

    data = copy.deepcopy(settings)
    for section, key in SECRET_PATHS:
        data.get(section, {}).pop(key, None)
    return data


def export_data() -> dict[str, Any]:
    return {
        "molly_settings_version": 1,
        "settings": public_view(get()),
    }


def import_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Импорт настроек из экспорта. Секреты и состояние LAN не затрагиваются."""

    if not isinstance(payload, dict):
        raise ValueError("файл настроек должен быть JSON-объектом")

    data = payload.get("settings", payload)

    if not isinstance(data, dict):
        raise ValueError("в файле нет раздела settings")

    data = copy.deepcopy(data)

    for section, key in SECRET_PATHS:
        if isinstance(data.get(section), dict):
            data[section].pop(key, None)

    data.pop("setup_done", None)

    return update(data)


# ------------------------------------------------------------
# Резервные копии (ручное управление)
# ------------------------------------------------------------

_BACKUP_NAME = re.compile(r"^settings_[0-9\-]+\.json$")


def list_backups() -> list[dict[str, Any]]:

    if not BACKUP_DIR.exists():
        return []

    result = []

    for path in sorted(BACKUP_DIR.glob("settings_*.json"), reverse=True):
        result.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "modified": int(path.stat().st_mtime),
            }
        )

    return result


def backup_now() -> str:

    with _lock:

        data = get()

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")

        name = f"settings_{stamp}.json"

        (BACKUP_DIR / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        old = sorted(BACKUP_DIR.glob("settings_*.json"))

        for path in old[:-BACKUP_KEEP]:
            path.unlink(missing_ok=True)

    return name


def restore_backup(name: str) -> dict[str, Any]:

    if not _BACKUP_NAME.match(name or ""):
        raise ValueError("Недопустимое имя резервной копии.")

    path = BACKUP_DIR / name

    if not path.exists():
        raise ValueError("Резервная копия не найдена.")

    data = _read_file(path)

    # Секреты LAN (токен) при восстановлении сохраняем текущие.
    current = get()

    data.setdefault("lan", {})
    data["lan"] = {**data["lan"], "token": current["lan"].get("token", ""), "enabled": current["lan"]["enabled"]}

    return update(data)
