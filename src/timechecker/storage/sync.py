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

# id сохраняется при репликации → конфликт по PK `id` идемпотентен и для NULL-able natural-ключей
# (например activity_event.external_id бывает NULL — natural-конфликт его не ловит, re-push упал бы
# на дубле PK). commit_task — без id, по композитному PK.
_CONFLICT: dict[str, tuple[list[str], str]] = {
    "employee": (["id"], "update"),
    "project": (["id"], "update"),
    "task": (["id"], "update"),
    "ingest_run": (["id"], "update"),
    "claude_session": (["id"], "update"),
    "git_commit": (["id"], "update"),
    "commit_task": (["commit_id", "task_id"], "nothing"),
    "plane_transition": (["id"], "update"),
    "activity_event": (["id"], "update"),
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
    """Агрегаты: delete-replace по (employee_id, work_date) из SQLite — АТОМАРНО (один commit)."""
    days = set()
    for t in _DAILY:
        for r in src._query(f"SELECT DISTINCT employee_id, work_date FROM {t}"):
            days.add((r["employee_id"], r["work_date"]))
    rows_by = {t: src._query(f"SELECT * FROM {t}") for t in _DAILY}
    total = 0
    with dst.conn.cursor() as cur:
        for emp, wd in days:
            for t in _DAILY:
                cur.execute(f"DELETE FROM {t} WHERE employee_id=%s AND work_date=%s", (emp, wd))
        for t in _DAILY:
            rows = rows_by[t]
            if not rows:
                continue
            cols = list(rows[0].keys())
            ph = ",".join(["%s"] * len(cols))
            cur.executemany(f'INSERT INTO {t} ({", ".join(cols)}) VALUES ({ph})',
                            [tuple(r[c] for c in cols) for r in rows])
            total += len(rows)
    dst.conn.commit()  # delete+insert в одной транзакции → нет окна пустых партиций при сбое
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
