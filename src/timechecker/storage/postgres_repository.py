"""Postgres-backend репозитория (E6, TIME-36): примитивы поверх psycopg.

Плейсхолдер ``?`` → ``%s``; id через ``RETURNING id``. ``prepare_threshold=None`` —
совместимость с PgBouncer transaction pooler (Supabase, порт 6543).
"""

from __future__ import annotations

from typing import Any

from .base import BaseSqlRepository
from .pg_schema import MIGRATIONS


class PostgresRepository(BaseSqlRepository):
    """Реализация repository-интерфейса на Postgres (psycopg)."""

    MIGRATIONS = MIGRATIONS

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @classmethod
    def open(cls, db_url: str) -> PostgresRepository:
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(db_url, row_factory=dict_row, prepare_threshold=None)
        repo = cls(conn)
        repo.apply_migrations()
        return repo

    def close(self) -> None:
        self.conn.close()

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def _exec(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(self._q(sql), params)
        self.conn.commit()

    def _query(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(self._q(sql), params)
            return cur.fetchall()

    def _fetchone(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(self._q(sql), params)
            return cur.fetchone()

    def _insert(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(self._q(sql) + " RETURNING id", params)
            row = cur.fetchone()
        self.conn.commit()
        return int(row["id"])

    def _executescript(self, sql):
        with self.conn.cursor() as cur:
            for stmt in sql.split(";"):
                if stmt.strip():
                    cur.execute(stmt)
        self.conn.commit()

    def _delete_count(self, sql, params):
        with self.conn.cursor() as cur:
            cur.execute(self._q(sql), params)
            rc = cur.rowcount
        self.conn.commit()
        return rc
