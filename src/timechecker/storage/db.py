"""Подключение к SQLite и применение миграций (TIME-4)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schema import MIGRATIONS


def connect(path: Any) -> sqlite3.Connection:
    """Открыть соединение с SQLite (создаёт каталог), включить FK, Row-factory."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")  # ждать при конкуренции collect↔sync, не падать
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def current_version(conn: sqlite3.Connection) -> int:
    """Текущая применённая версия схемы (0, если миграций ещё нет)."""
    _ensure_migrations_table(conn)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Применить недостающие миграции по порядку. Идемпотентно. Вернуть итоговую версию."""
    _ensure_migrations_table(conn)
    applied = current_version(conn)
    now = datetime.now(UTC).isoformat()
    for version, sql in MIGRATIONS:
        if version > applied:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (version, now),
            )
            conn.commit()
            applied = version
    return applied


def init_db(path: Any) -> sqlite3.Connection:
    """Открыть БД и применить миграции; вернуть готовое соединение."""
    conn = connect(path)
    apply_migrations(conn)
    return conn
