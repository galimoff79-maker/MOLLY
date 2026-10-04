from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR
from database import MollyConnection
from logging_setup import get_logger

logger = get_logger("chats")

# ============================================================
# История чатов (data/chats.sqlite3)
# ============================================================
#
# owner — владелец чатов: "local" (компьютер, где запущена МОЛЛИ)
# или "lan:<имя>" (пользователь из локальной сети). Чужие чаты
# недоступны: все запросы фильтруются по owner.

CHATS_DB = DATA_DIR / "chats.sqlite3"

_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chats_owner ON chats(owner, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    sources_json TEXT,
    stopped INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:

    global _initialized

    CHATS_DB.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(CHATS_DB),
        timeout=30,
        check_same_thread=False,
        factory=MollyConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    if not _initialized:
        with _init_lock:
            if not _initialized:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                _initialized = True

    return conn


def create_chat(owner: str, title: str = "Новый чат") -> dict[str, Any]:

    now = _now()

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO chats (owner, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (owner, title[:120] or "Новый чат", now, now),
        )
        conn.commit()
        chat_id = int(cur.lastrowid)

    return {"id": chat_id, "title": title[:120] or "Новый чат", "updated_at": now}


def list_chats(owner: str, limit: int = 300) -> list[dict[str, Any]]:

    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM chats "
            "WHERE owner = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (owner, limit),
        ).fetchall()

    return [dict(r) for r in rows]


def chat_exists(owner: str, chat_id: int) -> bool:

    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM chats WHERE id = ? AND owner = ?",
            (chat_id, owner),
        ).fetchone() is not None


def get_chat(owner: str, chat_id: int) -> dict[str, Any] | None:

    with _connect() as conn:
        chat = conn.execute(
            "SELECT id, title, updated_at FROM chats WHERE id = ? AND owner = ?",
            (chat_id, owner),
        ).fetchone()

        if chat is None:
            return None

        rows = conn.execute(
            "SELECT id, role, content, sources_json, stopped, created_at "
            "FROM messages WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()

    messages = []
    for r in rows:
        item = dict(r)
        sources = item.pop("sources_json")
        item["sources"] = json.loads(sources) if sources else []
        item["stopped"] = bool(item["stopped"])
        messages.append(item)

    result = dict(chat)
    result["messages"] = messages
    return result


def rename_chat(owner: str, chat_id: int, title: str) -> bool:

    title = (title or "").strip()[:120]

    if not title:
        raise ValueError("Название не может быть пустым")

    with _connect() as conn:
        cur = conn.execute(
            "UPDATE chats SET title = ? WHERE id = ? AND owner = ?",
            (title, chat_id, owner),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_chat(owner: str, chat_id: int) -> bool:

    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM chats WHERE id = ? AND owner = ?",
            (chat_id, owner),
        )
        conn.commit()
        return cur.rowcount > 0


def add_message(
    chat_id: int,
    role: str,
    content: str,
    sources: list[dict[str, Any]] | None = None,
    stopped: bool = False,
) -> int:

    now = _now()

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (chat_id, role, content, sources_json, stopped, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                role,
                content,
                json.dumps(sources, ensure_ascii=False) if sources else None,
                1 if stopped else 0,
                now,
            ),
        )
        conn.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ?",
            (now, chat_id),
        )
        conn.commit()
        return int(cur.lastrowid)


def recent_messages(chat_id: int, limit: int) -> list[dict[str, str]]:
    """Последние сообщения чата в хронологическом порядке."""

    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? "
            "AND content <> '' ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()

    return [
        {"role": r["role"], "content": r["content"]}
        for r in reversed(rows)
    ]
