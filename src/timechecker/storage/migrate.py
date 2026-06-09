"""Перенос данных SQLite → Postgres (E6, TIME-38).

Копирует все таблицы с сохранением id (Postgres-схема — IDENTITY BY DEFAULT, допускает явный id),
батч-вставкой (``executemany``), идемпотентно (``ON CONFLICT DO NOTHING``), затем выравнивает
IDENTITY-секвенсы под max(id). Порядок таблиц учитывает внешние ключи.
"""

from __future__ import annotations

from typing import Any

_TABLES = [
    "employee", "project", "task", "ingest_run", "activity_event", "claude_session",
    "git_commit", "commit_task", "plane_transition", "daily_summary", "daily_task_time",
    "daily_idle",
]


def migrate_sqlite_to_postgres(src: Any, dst: Any) -> dict:
    """Перенести все таблицы из SQLite-репозитория ``src`` в Postgres-репозиторий ``dst``."""
    counts: dict[str, int] = {}
    for table in _TABLES:
        rows = src._query(f"SELECT * FROM {table}")
        counts[table] = len(rows)
        if not rows:
            continue
        cols = list(rows[0].keys())
        ph = ",".join(["%s"] * len(cols))
        sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({ph}) ON CONFLICT DO NOTHING"
        params = [tuple(r[c] for c in cols) for r in rows]
        with dst.conn.cursor() as cur:
            cur.executemany(sql, params)
        dst.conn.commit()
    for table in _TABLES:
        if table == "commit_task":
            continue
        dst._exec(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"GREATEST((SELECT COALESCE(MAX(id), 0) FROM {table}), 1))")
    return counts
