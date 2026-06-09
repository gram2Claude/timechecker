"""Инкрементальная репликация SQLite → Postgres/Supabase (local-first, E8).

SQLite — источник правды; Supabase — облачная копия-**архив** (superset): новые/изменённые строки
доливаются идемпотентно, с сохранением SQLite-`id` (FK-консистентность). Локальные удаления
(`prune`) НЕ реплицируются — облако хранит полную историю. Baseline — `sync --reset` (TRUNCATE +
ресед). Перенос батчами (`executemany`); watermark двигается только после commit на стороне PG.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# Порядок вставки учитывает внешние ключи (родители раньше детей).
_REF = ["employee", "project", "task", "ingest_run"]
_RAW_SMALL = ["claude_session", "git_commit", "commit_task", "plane_transition"]
_DAILY = ["daily_summary", "daily_task_time", "daily_idle"]
_ALL = [*_REF, "activity_event", *_RAW_SMALL, *_DAILY]

# таблица → (ключ конфликта, режим): "update" = DO UPDATE неключевых; "nothing" = DO NOTHING.
_CONFLICT: dict[str, tuple[list[str], str]] = {
    "employee": (["windows_username"], "update"),
    "project": (["slug"], "update"),
    "task": (["plane_identifier"], "update"),
    "ingest_run": (["id"], "update"),
    "claude_session": (["session_uid"], "update"),
    "git_commit": (["sha"], "update"),
    "commit_task": (["commit_id", "task_id"], "nothing"),
    "plane_transition": (["external_id"], "update"),
    "activity_event": (["source", "external_id"], "update"),
}

_BATCH = 2000


def _ensure_sync_state(src: Any) -> None:
    src._exec("CREATE TABLE IF NOT EXISTS sync_state ("
              "table_name TEXT PRIMARY KEY, last_id INTEGER NOT NULL DEFAULT 0, last_sync_at TEXT)")


def _get_wm(src: Any, table: str) -> int:
    row = src._fetchone("SELECT last_id FROM sync_state WHERE table_name=?", (table,))
    return int(row["last_id"]) if row else 0


def _set_wm(src: Any, table: str, last_id: int, now: str) -> None:
    src._exec("INSERT INTO sync_state(table_name, last_id, last_sync_at) VALUES(?,?,?) "
              "ON CONFLICT(table_name) DO UPDATE SET last_id=excluded.last_id, "
              "last_sync_at=excluded.last_sync_at", (table, last_id, now))


def _push(dst: Any, table: str, rows: list[dict], conflict: list[str] | None, mode: str) -> int:
    """Батч-вставка строк в Postgres. conflict=None → обычный INSERT (после delete)."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ",".join(["%s"] * len(cols))
    if not conflict:
        tail = ""
    elif mode == "nothing":
        tail = f"ON CONFLICT ({', '.join(conflict)}) DO NOTHING"
    else:
        sets = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in conflict)
        tail = f"ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET {sets}"
    sql = f'INSERT INTO {table} ({", ".join(cols)}) VALUES ({placeholders}) {tail}'.strip()
    pushed = 0
    for i in range(0, len(rows), _BATCH):
        chunk = rows[i:i + _BATCH]
        params = [tuple(r[c] for c in cols) for r in chunk]
        with dst.conn.cursor() as cur:
            cur.executemany(sql, params)
        dst.conn.commit()
        pushed += len(chunk)
    return pushed


def _sync_events(src: Any, dst: Any, *, full: bool, lookback_days: int, now: str) -> int:
    """Лента: инкрементально по id + окно ts для backfill task_id (только non-null external)."""
    conflict = _CONFLICT["activity_event"][0]
    wm = 0 if full else _get_wm(src, "activity_event")
    if full:
        rows = src._query("SELECT * FROM activity_event ORDER BY id")
    else:
        cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = src._query(
            "SELECT * FROM activity_event "
            "WHERE id > ? OR (ts_utc >= ? AND external_id IS NOT NULL) ORDER BY id", (wm, cutoff))
    n = _push(dst, "activity_event", rows, conflict, "update")
    if rows:
        _set_wm(src, "activity_event", max(wm, max(r["id"] for r in rows)), now)
    return n


def _sync_daily(src: Any, dst: Any) -> int:
    """Агрегаты: delete-replace по всем (employee_id, work_date), что есть в SQLite."""
    days = set()
    for t in _DAILY:
        for r in src._query(f"SELECT DISTINCT employee_id, work_date FROM {t}"):
            days.add((r["employee_id"], r["work_date"]))
    with dst.conn.cursor() as cur:
        for emp, wd in days:
            for t in _DAILY:
                cur.execute(f"DELETE FROM {t} WHERE employee_id=%s AND work_date=%s", (emp, wd))
    dst.conn.commit()
    total = 0
    for t in _DAILY:
        total += _push(dst, t, src._query(f"SELECT * FROM {t}"), None, "plain")
    return total


def _align_sequences(dst: Any) -> None:
    """Выровнять IDENTITY-секвенсы под max(id) после вставки явных id."""
    for t in _ALL:
        if t == "commit_task":
            continue
        with dst.conn.cursor() as cur:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {t}), 1), (SELECT MAX(id) FROM {t}) IS NOT NULL)")
        dst.conn.commit()


def reset_postgres(dst: Any) -> None:
    """TRUNCATE всех timechecker-таблиц (кроме schema_migrations) — для чистого baseline."""
    with dst.conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(_ALL)} RESTART IDENTITY CASCADE")
    dst.conn.commit()


def sync_to_postgres(src: Any, dst: Any, *, full: bool = False, reset: bool = False,
                     lookback_days: int = 2) -> dict:
    """Реплицировать SQLite ``src`` → Postgres ``dst``. Возвращает счётчики по таблицам."""
    _ensure_sync_state(src)
    now = datetime.now(UTC).isoformat()
    if reset:
        reset_postgres(dst)
        full = True
    counts: dict[str, int] = {}
    for t in [*_REF, *_RAW_SMALL]:
        conflict, mode = _CONFLICT[t]
        counts[t] = _push(dst, t, src._query(f"SELECT * FROM {t}"), conflict, mode)
    counts["activity_event"] = _sync_events(
        src, dst, full=full, lookback_days=lookback_days, now=now)
    counts["daily"] = _sync_daily(src, dst)
    _align_sequences(dst)
    return counts
