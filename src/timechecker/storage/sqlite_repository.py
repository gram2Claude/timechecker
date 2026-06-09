"""SQLite-backend репозитория (E6, TIME-36): примитивы поверх sqlite3 + миграции."""

from __future__ import annotations

import sqlite3
from typing import Any

from .base import BaseSqlRepository
from .db import connect
from .schema import MIGRATIONS


class SqliteRepository(BaseSqlRepository):
    """Реализация repository-интерфейса на SQLite (плейсхолдер ``?`` — нативный)."""

    MIGRATIONS = MIGRATIONS

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: Any) -> SqliteRepository:
        repo = cls(connect(path))
        repo.apply_migrations()
        return repo

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql, params=()):
        self.conn.execute(sql, params)
        self.conn.commit()

    def _query(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _fetchone(self, sql, params=()):
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r is not None else None

    def _insert(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.lastrowid)

    def _executescript(self, sql):
        self.conn.executescript(sql)
        self.conn.commit()

    def _delete_count(self, sql, params):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.rowcount
